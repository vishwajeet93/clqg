
Configuration Files:
1. config_generator_train_BPE.yaml - Contains the configuration to train the supervised Question generation task.
2. config_generator_pretrain_BPE.yaml - Contains the configuration to train the unsupervised task (with back translation)



To run unsupervised pretraining:
```
python train_unsupervised.py -c configs/config_generator_pretrain_BPE.yaml
```

To train the supervised question generation task:
```
python train_qgen.py -c configs/config_generator_train_BPE.yaml
```



