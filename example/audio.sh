#!/bin/bash -l

#SBATCH --job-name="nlp"
#SBATCH --account=admalware
#SBATCH --partition=tier3
#SBATCH --output=./logs/%x_%j.out
#SBATCH --time=2-00:00:00
#SBATCH --nodes=1
#SBATCH --cpus-per-task=1
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:a100:4
#SBATCH --mem=32G


source ~/anaconda3/etc/profile.d/conda.sh
conda activate MDALTH

torchrun --standalone --nnodes=1 --nproc-per-node=1 ./example/main.py \
--task="audio" \
--learn \
--evaluate \
--dataset="PolyAI/minds14" \
--pretrained_model_name_or_path="facebook/wav2vec2-base" \
--querier="random" \
--stopper="null" \
--n_iterations=16 \
--log_level="warning" \
--n_start=32 \
--n_query=32 \
--output_dir="WILL_BE_IGNORED" \
--learning_rate="3e-5" \
--per_device_train_batch_size=64 \
--per_device_eval_batch_size=64 \
--auto_find_batch_size \
--gradient_accumulation_steps=4 \
--num_train_epochs=25 \
--weight_decay=0.01 \
--evaluation_strategy="epoch" \
--save_strategy="epoch" \
--load_best_model_at_end \
--save_total_limit=1 \
--optim="adamw_torch" \
--warmup_ratio=0.1 \
--group_by_length \
--dataloader_num_workers=16 \
--dataloader_pin_memory \
--logging_strategy="epoch" \
--fp16=True
