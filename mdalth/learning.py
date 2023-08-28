"""
Active learning loop.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
from pathlib import Path
from pprint import pformat
import warnings
import shutil
from typing import Optional  # , Self  # FIXME: import

from datasets import Dataset, DatasetDict
import numpy as np
from transformers import (
    AutoModelForSequenceClassification,
    PreTrainedModel,
    Trainer,
)
from transformers.trainer_utils import TrainOutput

from mdalth.helpers import IOHelper, Pool, TrainerFactory
from mdalth.querying import Querier, RandomQuerier
from mdalth.querying_wrappers import querier_wrapper_factory, QuerierWrapper
from mdalth.stopping import Stopper, NullStopper

from mdalth.tp import ProportionOrInteger
from mdalth.utils import get_highest_path, load_with_pickle, save_with_pickle, proportion_or_integer_to_int


def compute_total_al_iterations(n_rows: int, n_start: int, n_query: int) -> int:
    q, r = divmod(n_rows - n_start, n_query)
    if r == 0:
        return q
    return q + 1


# TODO: refactor scripting args into their own dataclass
@dataclass
class Config:
    """Configures the active learning loop."""

    n_rows: Optional[int] = field(
        default=None,
        metadata={
            "help": "Number of rows in the dataset. Must be passed in `__init__` or `configure`."
        },
    )
    n_start: ProportionOrInteger = field(
        default=0.1, metadata={"help": "Number of examples to inititally randomly label."}
    )
    n_query: ProportionOrInteger = field(
        default=0.1, metadata={"help": "Number of examples to query per iteration."}
    )
    val_set_size: ProportionOrInteger = field(
        default=0.1, metadata={"help": "Size of the randomly selected validation set."}
    )
    n_iterations: ProportionOrInteger = field(
        default=1.0, metadata={"help": "Optionally run a subset of AL iterations."}
    )
    learn: bool = field(
        default=False,
        metadata={"help": "Whether to run the training loop. Used for scripting."},
    )
    evaluate: bool = field(
        default=False,
        metadata={"help": "Whether to run the evaluation loop. Used for scripting."},
    )
    resume: bool = field(
        default=False, metadata={"help": "Resume from previous stage of progress. Used for scripting."}
    )
    resume_from_checkpoint: Optional[int] = field(
        default=0, metadata={"help": "Checkpoint to resume. If None, resume from latest. Used for scripting."}
    )

    def __post_init__(self) -> None:
        self._configured = False
        if self.n_rows:
            self.configure(self.n_rows)
            self._configured = True

    def configure(self, n_rows: int) -> Config:
        assert not self._configured, "If configuring, Config must not be configured!"
        self.n_rows = n_rows
        self.n_start = proportion_or_integer_to_int(self.n_start, self.n_rows)
        self.n_query = proportion_or_integer_to_int(self.n_query, self.n_rows)
        total = compute_total_al_iterations(self.n_rows, self.n_start, self.n_query)
        self.n_iterations = proportion_or_integer_to_int(self.n_iterations, total)
        if self.n_iterations > total:
            warnings.warn(
                f"Configured to run {self.n_iterations} iterations, but only {total} are "
                f"possible. Instead, setting `n_iterations` to {total}."
            )
            self.n_iterations = total
        self._configured = True
        return self

    # TODO: move to function
    def validation_set_size(self, num_labeled: Optional[int] = None) -> int:
        assert self._configured, "Config must be configured!"
        if isinstance(self.val_set_size, int):
            return self.val_set_size
        if isinstance(self.val_set_size, float) and num_labeled is not None:
            return int(self.val_set_size * num_labeled)
        raise RuntimeError()

    # TODO: move to function
    def output_root(self) -> Path:
        assert self._configured, "Config must be configured!"
        others = [self.n_start, self.n_query, self.val_set_size]
        others = list(map(str, others))
        return Path("").joinpath(*others)


# TODO: should these be optional?
@dataclass
class LearnerState:
    """Mutable data from a single iteration of the active learning loop."""

    batch: np.ndarray
    dataset: DatasetDict
    iteration: int
    trainer: Trainer
    train_output: TrainOutput
    best_model: Optional[PreTrainedModel] = None

    def __post_init__(self) -> None:
        if self.trainer is not None:  # TODO: checkpointing system
            if self.trainer.args.load_best_model_at_end or self.best_model is None:
                self.best_model = self.trainer.model


class Learner:
    """Perform active learning.
    
    TODO
    ----
        - add an optional feature to label all data and train model at the end.
    """

    def __init__(
        self,
        pool: Pool,
        config: Config,
        io_helper: IOHelper,
        trainer_fact: TrainerFactory,
        querier: Optional[Querier] = None,
        stopper: Optional[Stopper] = None,
        _iteration: int = 0,
    ) -> None:
        self.pool = pool
        self.config = config
        self.io_helper = io_helper
        self.trainer_fact = trainer_fact
        self.iteration = _iteration
        if not self.io_helper.valid:
            raise FileExistsError(self.io_helper.root_path)
        querier = RandomQuerier() if querier is None else querier
        self.querier = querier_wrapper_factory(querier, pool, trainer_fact)
        stopper = NullStopper() if stopper is None else stopper
        # TODO: wrap the stopper in a StopperWrapper.
        self.stopper = stopper
        self.state = None

    def __call__(self) -> LearnerState:
        if self.iteration != 0:
            raise RuntimeError("The zeroth iteration has already been run.")
        self.pre()
        batch = self.query_first()
        dataset, trainer, train_output = self.train(batch)
        self.save_to_disk(batch, trainer.model, train_output)
        self.post()
        self.iteration += 1
        self.state = LearnerState(batch, dataset, self.iteration, trainer, train_output)
        return self.state

    def __iter__(self) -> Learner:
        return self

    def __len__(self) -> int:
        return self.config.n_iterations

    def __next__(self) -> LearnerState:
        if self.iteration == 0:
            raise RuntimeError("The zeroth iteration has not been run.")
        if self.iteration > len(self):
            raise StopIteration()
        self.pre()
        batch = self.query()
        dataset, trainer, train_output = self.train(batch)
        self.save_to_disk(batch, trainer.model, train_output)
        self.post()
        self.iteration += 1
        self.state = LearnerState(batch, dataset, self.iteration, trainer, train_output)
        return self.state

    @property
    def dataset(self) -> Dataset:
        return self.pool.dataset

    @property
    def num_rows(self) -> int:
        return self.dataset.num_rows

    # TODO: checkpointing
    @classmethod
    def load_from_disk(
        cls,
        output_dir: Path,
        config: Config,
        trainer_fact: TrainerFactory,
        querier: Optional[Querier] = None,
        stopper: Optional[Stopper] = None,
        iteration: Optional[int] = None,
    ) -> Learner:
        warnings.warn("Checkpointing is not very much in a working state. Behavior undefined.")
        io_helper = IOHelper(output_dir, overwrite=True)
        if not iteration:
            iteration = int(get_highest_path(io_helper.iterations_path.glob("*")).name) - 1
        dataset = Dataset.load_from_disk(io_helper.tr_dataset_path)
        batches = [np.loadtxt(io_helper.batch_path(i)) for i in range(iteration)]
        # TODO: remove these hacks.
        batches = [b.astype(int) for b in batches]
        labeled_idx = np.concatenate(batches)
        pool = Pool.from_pools(dataset, labeled_idx)
        learner = cls(pool, config, io_helper, trainer_fact, querier, stopper, _iteration=iteration)
        learner.state = LearnerState(
            batches[-1],
            None,
            learner.iteration,
            None,
            None,
            AutoModelForSequenceClassification.from_pretrained(io_helper.model_path(iteration)),
        )
        return learner

    def save_to_disk(
        self, batch: np.ndarray, model: PreTrainedModel, train_output: TrainOutput
    ) -> None:
        if self.iteration == 0:
            self.io_helper.mkdir(exist_ok=True)
            save_with_pickle(self.io_helper.config_path, self.config)
            self.dataset.save_to_disk(self.io_helper.tr_dataset_path)

        np.savetxt(self.io_helper.batch_path(self.iteration), batch, fmt="%i")
        model.save_pretrained(self.io_helper.model_path(self.iteration))
        save_with_pickle(self.io_helper.trainer_output_path(self.iteration), train_output)

    def train(self, batch: np.ndarray) -> tuple[Dataset, Trainer, TrainOutput]:
        self.pool.label(batch)
        test_size = self.config.validation_set_size(len(self.pool.labeled_idx))
        dataset = self.pool.labeled.train_test_split(test_size=test_size)
        trainer = self.trainer_fact(dataset["train"], dataset["test"])
        trainer.args.output_dir = self.io_helper.checkpoints_path(self.iteration)
        train_output = trainer.train()
        return dataset, trainer, train_output

    def query_first(self) -> np.ndarray:
        return RandomQuerier()(self.config.n_start, self.pool.unlabeled_idx)

    def pre(self) -> None:
        """Subclass and override to inject custom behavior."""

    def post(self) -> None:
        """Subclass and override to inject custom behavior."""
        shutil.rmtree(self.io_helper.checkpoints_path(self.iteration))

    def stop(self) -> bool:
        """Subclass and override to inject custom behavior."""
        return self.stopper()

    def query(self) -> np.ndarray:
        """Subclass and override to inject custom behavior."""
        return self.querier(self.config.n_query, self.state.best_model)


class Evaluator:
    """Evaluate trained models on a test set.
    
    TODO
    ----
        - consolidate the various files into simpler datastructures.
    """

    def __init__(
        self,
        ts_trainer_fact: TrainerFactory,
        ts_dataset: Dataset,
        io_helper: IOHelper,
        _iteration: int = 0,
    ) -> None:
        self.ts_trainer_fact = ts_trainer_fact
        self.ts_dataset = ts_dataset
        self.io_helper = io_helper
        if not self.io_helper.exists(ts_dataset=False):
            raise FileNotFoundError(self.io_helper.root_path)
        self.iteration = _iteration
        self.config: Config = load_with_pickle(io_helper.config_path)
        self.pool = Pool(Dataset.load_from_disk(io_helper.tr_dataset_path))

    def __call__(self) -> Evaluator:
        return self

    def __iter__(self) -> Learner:
        return self

    def __len__(self) -> int:
        return self.config.n_iterations

    def __next__(self) -> tuple[PreTrainedModel, TrainOutput]:
        if self.iteration > len(self):
            raise StopIteration()
        self.pre()
        batch = np.loadtxt(self.io_helper.batch_path(self.iteration), dtype=np.int64)
        self.pool.label(batch)
        trainer, results = self.eval()
        self.save_to_disk(results)
        self.post()
        self.iteration += 1
        return trainer.model, results

    @property
    def tr_dataset(self) -> Dataset:
        return self.pool.dataset

    @property
    def tr_num_rows(self) -> int:
        return self.tr_dataset.num_rows

    @property
    def ts_num_rows(self) -> int:
        return self.ts_dataset.num_rows

    # TODO: checkpointing system.
    @classmethod
    def load_from_disk(cls, output_dir: Path, iteration: Optional[int] = None) -> Evaluator:
        warnings.warn("Checkpointing is not very much in a working state. Behavior undefined.")
        io_helper = IOHelper(output_dir)
        if not iteration:
            complete = []
            for p in io_helper.iterations_path.glob("*"):
                if io_helper._test_metrics in [p_.name for p_ in p.iterdir()]:
                    complete.append(io_helper.iterations_path / p.name)
            iteration = get_highest_path(complete)

    def save_to_disk(self, results: dict[str, float]) -> None:
        if self.iteration == 0:
            self.ts_dataset.save_to_disk(self.io_helper.ts_dataset_path)
        with open(self.io_helper.test_metrics_path(self.iteration), "w") as f:
            json.dump(results, f)

    def eval(self) -> tuple[Trainer, dict[str, float]]:
        model = AutoModelForSequenceClassification.from_pretrained(
            self.io_helper.model_path(self.iteration)
        )
        ts_trainer = self.ts_trainer_fact(None, self.ts_dataset, model)
        results = ts_trainer.evaluate(self.ts_dataset)
        return ts_trainer, results

    def pre(self) -> None:
        ...

    def post(self) -> None:
        ...
