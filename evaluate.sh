#!/usr/bin/env bash
export CUDA_VISIBLE_DEVICES='2'

python evaluate.py -c ./configs/config_generator_train.yaml
