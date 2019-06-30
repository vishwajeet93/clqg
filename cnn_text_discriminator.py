# this code is implemented as a discriminator to classify the sentence

import tensorflow as tf
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import variable_scope as vs

#from data_iterator import domainTextIterator
from data_iterator import unSuperGanTextIterator
from data_iterator import disThreeTextIterator
from data_iterator import disTextIterator
from share_function import dis_length_prepare
from share_function import average_clip_gradient
from share_function import average_clip_gradient_by_value
from share_function import dis_three_length_prepare
from model import split_tensor


import time
import numpy
import os

from tensorflow.contrib.layers.python.layers import batch_norm as batch_norm

def conv_batch_norm(x, is_train, scope='bn', decay=0.9, reuse_var = False):

    out = batch_norm(x, 
               decay=decay,
               center=True, 
               scale=True, 
               updates_collections=None,
               is_training=is_train,
               reuse=reuse_var,
               trainable=True,
               scope=scope)
    return out

def linear(inputs, output_size, use_bias, scope='linear'):
    if not scope:
        scope=tf.get_variable_scope()

    input_size = inputs.get_shape()[1].value
    dtype=inputs.dtype

    with tf.variable_scope(scope):
        weights=tf.get_variable('weights', [input_size, output_size], dtype=dtype)
        res = tf.matmul(inputs, weights)
        if not use_bias:
            return res
        biases=tf.get_variable('biases', [output_size], dtype=dtype)
    return tf.add(res, biases)

def highway(input_, size, layer_size=1, bias=-2, f=tf.nn.relu, reuse_var=False):
    output = input_
    if reuse_var == True:
        tf.get_variable_scope().reuse_variables()
    for idx in xrange(layer_size):
        output = f(linear(output, size, 0, scope='output_lin_%d' %idx))
        transform_gate = tf.sigmoid(linear(input_, size, 0, scope='transform_lin_%d'%idx) +bias)
        carry_gate = 1. - transform_gate
        output = transform_gate * output + carry_gate * input_
    return output

def highway_s(input_, size, layer_size=1, bias=-2, f=tf.nn.relu, reuse_var=False):
    output = input_
    if reuse_var == True:
        tf.get_variable_scope().reuse_variables()
    for idx in xrange(layer_size):
        output = f(linear(output, size, 0, scope='output_s_lin_%d' %idx))
        transform_gate = tf.sigmoid(linear(input_, size, 0, scope='transform_s_lin_%d'%idx) +bias)
        carry_gate = 1. - transform_gate
        output = transform_gate * output + carry_gate * input_
    return output

class cnn_layer(object):
    def __init__(self, filter_size, dim_word, num_filter, scope='cnn_layer', init_device='/cpu:0', reuse_var=False):
        self.filter_size = filter_size
        self.dim_word = dim_word
        self.num_filter = num_filter
        self.scope = scope
        self.reuse_var = reuse_var
        if reuse_var == False:
            with tf.variable_scope(self.scope or 'cnn_layer'):
                with tf.variable_scope('self_model'):
                    with tf.device(init_device):
                        filter_shape = [filter_size, dim_word, 1, num_filter]
                        b = tf.get_variable('b', initializer = tf.constant(0.1, shape=[num_filter]))
                        W = tf.get_variable('W', initializer = tf.truncated_normal(filter_shape, stddev=0.1))

    ## convolutuon with batch normalization
    def conv_op(self, input_sen, stride, is_train, padding='VALID', is_batch_norm = True, f_activation=tf.nn.relu):
         with tf.variable_scope(self.scope):
           with tf.variable_scope('self_model'):
               tf.get_variable_scope().reuse_variables()
               b = tf.get_variable('b')
               W = tf.get_variable('W')
               conv = tf.nn.conv2d(
               input_sen,
               W,
               stride,
               padding,
               name='conv')
               bias_add = tf.nn.bias_add(conv, b)
           
           if is_batch_norm :
               with tf.variable_scope('conv_batch_norm'):
                 conv_bn = conv_batch_norm(bias_add, is_train = is_train, scope='bn', reuse_var = self.reuse_var)
               h = f_activation(conv_bn, name='relu')
           else:
               h = f_activation(bias_add, name='relu')

         return h
        
class text_DisCNN(object):
  """
  A CNN for sentence classification
  Uses an embedding layer, followed by a convolutional layer, max_pooling and softmax layer.
  vocab_size_s: the size of Chinese vocab
  vocab_size_t: the size of English vocab
  source_dict: Chinese dict
  target_dict: English dict
  s_domain_data: Chinese data
  t_domain_data: English data
  g_domain_data: generated data
  """

  def __init__(self, sess, max_len, num_classes, vocab_size_s,  batch_size,  dim_word, filter_sizes, num_filters, source_dict, gpu_device, s_domain_data, s_domain_generated_data, dev_s_domain_data, dev_s_domain_generated_data=None, max_epoches=10, dispFreq = 1, saveFreq = 10, devFreq=1000, clip_c = 1.0, optimizer='adadelta', saveto='text_discriminator', reload=False, reshuffle = False, l2_reg_lambda=0.0, scope='text_discnn', init_device="/cpu:0", reuse_var=False):
    
        self.sess = sess
        self.max_len = max_len
        self.num_classes = num_classes
        self.vocab_size_s = vocab_size_s
        self.dim_word = dim_word
        self.filter_sizes = filter_sizes
        self.num_filters = num_filters
        self.l2_reg_lambda = l2_reg_lambda
        self.num_filters_total = sum(self.num_filters)
        self.scope = scope
        self.s_domain_data = s_domain_data
        self.s_domain_generated_data = s_domain_generated_data
        self.dev_s_domain_data = dev_s_domain_data
        self.dev_s_domain_generated_data = dev_s_domain_generated_data
        self.reshuffle = reshuffle
        self.batch_size = batch_size
        self.max_epoches = max_epoches
        self.dispFreq = dispFreq
        self.saveFreq = saveFreq
        self.devFreq = devFreq
        self.clip_c = clip_c
        self.saveto = saveto
        self.reload = reload
        
        print('num_filters_total is ', self.num_filters_total)

        if optimizer == 'adam':
            self.ptimizer = tf.train.AdamOptimizer()
            print("using adam as the optimizer for the discriminator")
        elif optimizer == 'adadelta':
            self.optimizer = tf.train.AdadeltaOptimizer(learning_rate=1.,rho=0.95,epsilon=1e-6)
            print("using adadelta as the optimizer for the discriminator")
        elif optimizer == 'sgd':
            self.optimizer = tf.train.GradientDescentOptimizer(0.0001)
            print("using sgd as the optimizer for the discriminator")
        elif optimizer == 'rmsprop':
            self.optimizer = tf.train.RMSPropOptimizer(0.0001)
            print("using rmsprop as the optimizer for the discriminator")
        else :
            raise ValueError("optimizer must be adam, adadelta or sgd.")

        dictionaries=[]
        dictionaries.append(source_dict)
        self.dictionaries = dictionaries
      
        gpu_string = gpu_device
        gpu_devices = []
        gpu_devices = gpu_string.split('-')
        self.gpu_devices = gpu_devices[1:]
        self.gpu_num = len(self.gpu_devices)
        #print('the gpu_num is ', self.gpu_num)

        self.build_placeholder()
        
        if reuse_var == False:
           with tf.variable_scope(self.scope or 'disCNN'):
              with tf.variable_scope('model_self'):
                 with tf.device(init_device):
                    embeddingtable = tf.get_variable('embeddingtable', initializer = tf.random_uniform([self.vocab_size_s, self.dim_word], -1.0, 1.0))
                    W = tf.get_variable('W', initializer = tf.truncated_normal([self.num_filters_total, self.num_classes], stddev=0.1))
                    b = tf.get_variable('b', initializer = tf.constant(0.1, shape=[self.num_classes]))

       ## build_model ##########
        print('building train model')
        self.build_train_model()
        print('done')
        print('build_discriminate ')
        #self.build_discriminate(gpu_device=self.gpu_devices[-1])
        self.build_discriminator_model(dis_devices=self.gpu_devices)
        print('done')

        params = [param for param in tf.global_variables() if self.scope in param.name]
        if not self.sess.run(tf.is_variable_initialized(params[0])):
            init_op = tf.variables_initializer(params)
            self.sess.run(init_op)

        saver = tf.train.Saver(params)
        self.saver = saver

        if self.reload:
          print('reloading file from %s' % self.saveto)
          self.saver.restore(self.sess, self.saveto)
          print('reloading file done')


  def build_placeholder(self, gpu_num = None):
     self.x_list = []
     self.y_list = []
     self.drop_list = []
     if gpu_num is None:
         gpu_num = self.gpu_num

     for i in range(gpu_num):
        input_x = tf.placeholder(tf.int32, [self.max_len, None], name='input_x')
        input_y = tf.placeholder(tf.float32, [self.num_classes, None], name='input_y')
        drop_prob = tf.placeholder(tf.float32, name='dropout_prob')
        
        self.x_list.append(input_x)
        self.y_list.append(input_y)
        self.drop_list.append(drop_prob)

  def get_inputs(self, gpu_device):
     try:
        gpu_id = self.gpu_devices.index(gpu_device)
     except:
        raise ValueError('get inputs error!')   
     return self.x_list[gpu_id], self.y_list[gpu_id], self.drop_list[gpu_id]


  def build_model(self, reuse_var=False, gpu_device='0'):
     with tf.variable_scope(self.scope):
        with tf.device('/gpu:%d' % int(gpu_device)):    
            input_x, input_y, drop_keep_prob = self.get_inputs(gpu_device)

            input_x_trans = tf.transpose(input_x, [1,0])
            input_y_trans = tf.transpose(input_y, [1,0])

            with tf.variable_scope('model_self'):
                tf.get_variable_scope().reuse_variables()
                W = tf.get_variable('W')
                b = tf.get_variable('b')
                embeddingtable = tf.get_variable('embeddingtable')
    
            sentence_embed = tf.nn.embedding_lookup(embeddingtable, input_x_trans)
            sentence_embed_expanded = tf.expand_dims(sentence_embed, -1)
            pooled_outputs = []
            for filter_size, num_filter in zip(self.filter_sizes, self.num_filters):
                scope = "conv_maxpool-%s" % filter_size
                filter_shape = [filter_size, self.dim_word, 1, num_filter]
                strides=[1,1,1,1]
                conv = cnn_layer(filter_size, self.dim_word, num_filter, scope=scope, reuse_var = reuse_var)
                is_train = True
                conv_out = conv.conv_op(sentence_embed_expanded, strides, is_train=is_train)
                pooled = tf.nn.max_pool(conv_out, ksize=[1, (self.max_len - filter_size +1), 1, 1], strides=strides, padding='VALID', name='pool')
                pooled_outputs.append(pooled)
    
            h_pool = tf.concat(axis=3, values=pooled_outputs)
            h_pool_flat = tf.reshape(h_pool, [-1, self.num_filters_total])
            
            h_highway = highway(h_pool_flat, h_pool_flat.get_shape()[1], 1, 0, reuse_var=reuse_var)
            h_drop = tf.nn.dropout(h_highway, drop_keep_prob)
           

            scores = tf.nn.xw_plus_b(h_drop, W, b, name='scores')
            ypred_for_auc = tf.nn.softmax(scores)
            predictions = tf.argmax(scores, 1, name='prediction')
            losses = tf.nn.softmax_cross_entropy_with_logits(logits=scores, labels=input_y_trans)
           
            correct_predictions = tf.equal(predictions, tf.argmax(input_y_trans, 1))
            accuracy = tf.reduce_mean(tf.cast(correct_predictions, 'float'), name='accuracy')
           
            params = [param for param in tf.trainable_variables() if self.scope in param.name]
             
            #for param in params:
            #    print param.name

            #self.params = params

            grads_and_vars = self.optimizer.compute_gradients(losses, params)

            #for grad, var in grads_and_vars:
            #        print (var.name, grad)
            
            l2_loss = tf.constant(0.0)
            l2_loss += tf.nn.l2_loss(W)
            l2_loss += tf.nn.l2_loss(b)
            loss = tf.reduce_mean(losses) + self.l2_reg_lambda * l2_loss
            
            return input_x, input_y, drop_keep_prob, ypred_for_auc, predictions, loss, correct_predictions, accuracy, grads_and_vars
  
  def build_discriminator_body(self, input_x, input_y, dropout_keep_prob, reuse_var=True):

            input_x_trans = input_x
            input_y_trans = input_y
            dis_dropout_keep_prob = dropout_keep_prob
 
            with tf.variable_scope('model_self'):
                tf.get_variable_scope().reuse_variables()
                W = tf.get_variable('W')
                b = tf.get_variable('b')
                embeddingtable = tf.get_variable('embeddingtable')
    
            sentence_embed = tf.nn.embedding_lookup(embeddingtable, input_x_trans)
            
            sentence_embed_expanded = tf.expand_dims(sentence_embed, -1)

            pooled_outputs = []
    
            for filter_size, num_filter in zip(self.filter_sizes, self.num_filters):
                #print('the filter size is ', filter_size)
                scope = "conv_maxpool-%s" % filter_size
                filter_shape = [filter_size, self.dim_word, 1, num_filter]
                strides=[1,1,1,1]
                conv = cnn_layer(filter_size, self.dim_word, num_filter, scope=scope, reuse_var = reuse_var)
                is_train = False
                conv_out = conv.conv_op(sentence_embed_expanded, strides, is_train=is_train)
                pooled = tf.nn.max_pool(conv_out, ksize=[1, (self.max_len - filter_size +1), 1, 1], strides=strides, padding='VALID', name='pool')
                #print('the shape of the pooled is ', pooled.get_shape())
                pooled_outputs.append(pooled)
    
            h_pool = tf.concat(axis=3, values=pooled_outputs)
            #print('the shape of h_pool is ', h_pool.get_shape())
            #print('the num_filters_total is ', self.num_filters_total)

            h_pool_flat = tf.reshape(h_pool, [-1, self.num_filters_total])
           
            #print('the shape of h_pool_flat is ', h_pool_flat.get_shape())
            
            h_highway = highway(h_pool_flat, h_pool_flat.get_shape()[1], 1, 0, reuse_var=reuse_var)
            h_drop = tf.nn.dropout(h_highway, dis_dropout_keep_prob)

            scores = tf.nn.xw_plus_b(h_drop, W, b, name='scores')

            ypred_for_auc = tf.nn.softmax(scores)
            predictions = tf.argmax(scores, 1, name='prediction')
            losses = tf.nn.softmax_cross_entropy_with_logits(logits=scores, labels=input_y_trans)
           
            correct_predictions = tf.equal(predictions, tf.argmax(input_y_trans, 1))
            accuracy = tf.reduce_mean(tf.cast(correct_predictions, 'float'), name='accuracy')
           
            grads_and_vars = self.optimizer.compute_gradients(losses)
            
            l2_loss = tf.constant(0.0)
            l2_loss += tf.nn.l2_loss(W)
            l2_loss += tf.nn.l2_loss(b)
            loss = tf.reduce_mean(losses) + self.l2_reg_lambda * l2_loss
            
            return ypred_for_auc
            
  def build_discriminator_model(self, dis_devices):
      with tf.variable_scope(self.scope):
          with tf.device('/cpu:0'):
              self.dis_input_x = tf.placeholder(tf.int32, [self.max_len, None], name='input_x')
              self.dis_input_y = tf.placeholder(tf.float32, [self.num_classes, None], name='input_y')
              self.dis_dropout_keep_prob = tf.placeholder(tf.float32, name='dropout_keep_prob')

              dis_input_x = tf.transpose(self.dis_input_x, [1, 0])
              dis_input_y = tf.transpose(self.dis_input_y, [1, 0])

              devices = ['/gpu:' + i for i in dis_devices]
              
              input_x_list = split_tensor(dis_input_x, len(devices))
              input_y_list = split_tensor(dis_input_y, len(devices))
            
              dis_dropout_keep_prob = [self.dis_dropout_keep_prob] * len(devices)

              batch_size_list = [tf.shape(x)[0] for x in input_x_list]

              pred_list = [None] * len(devices)
              for i, (input_x, input_y, drop, device) in enumerate(zip(input_x_list, input_y_list, dis_dropout_keep_prob, devices)):
                  with tf.device(device):
                      print("building discriminator model on device %s" % device)
                      ypred_for_auc = self.build_discriminator_body(input_x, input_y, drop, reuse_var=True)
                      pred_list[i] = ypred_for_auc

              self.dis_ypred_for_auc = tf.concat(pred_list, axis=0)

          
  def build_train_model(self):
      loss = tf.convert_to_tensor(0.)
      grads = []
      accu = tf.convert_to_tensor(0.)
  
      reuse_var = False
      for i, gpu_device in enumerate(self.gpu_devices):
          #print('i is %d, gpu is %s' %(i, gpu_device))
          if i > 0:
              reuse_var = True
          #print('reuse_var is ', reuse_var)
          _, _, _, ypred_for_auc, predictions, losses, correct_predictions, accuracy, grads_and_vars  = self.build_model(reuse_var=reuse_var, gpu_device=gpu_device)
          loss += losses
          accu += accuracy
          grads.append(grads_and_vars)
  
      loss = loss / self.gpu_num
      accuracy = accu / self.gpu_num
      #grads_and_vars = average_clip_gradient(grads, self.clip_c)
      grads_and_vars = average_clip_gradient_by_value(grads, -1.0, 1.0)
      optm = self.optimizer.apply_gradients(grads_and_vars)

      clip_ops = []

      var_s = [var for var in tf.trainable_variables() if self.scope in var.name]
      for var in var_s:
          clip_ops.append(tf.assign(var, tf.clip_by_value(var, -1., 1.)))

      clip_ops = tf.group(*clip_ops)

      self.clip_ops = clip_ops

      self.train_loss = loss
      self.train_accuracy = accuracy
      self.train_grads_and_vars = grads_and_vars
      self.train_optm = optm
      self.train_ypred = ypred_for_auc
      

  def train(self, max_epoch = None, s_domain_data=None, s_domain_generated_data=None):
  
      if s_domain_data is None or s_domain_generated_data is None:
            s_domain_data = self.s_domain_data
            s_domain_generated_data = self.s_domain_generated_data
      
      print('the s_domain, s_domain_generated_data  is  %s, %s'  %(s_domain_data, s_domain_generated_data))

      if max_epoch is None:
            max_epoch = self.max_epoches

      def train_iter():
            Epoch = 0
            while True:
                if self.reshuffle:
                      os.popen('python shuffle.py ' + s_domain_data +' ' + s_domain_generated_data)
                      os.popen('mv ' + s_domain_data + '.shuf '+ s_domain_data)
                      os.popen('mv ' + s_domain_generated_data + '.shuf ' + s_domain_generated_data)

             
                disTrain = disTextIterator(s_domain_data, s_domain_generated_data,
                                              self.dictionaries[0],
                                              batch = self.batch_size * self.gpu_num,
                                              maxlen = self.max_len,
                                              n_words_target = self.vocab_size_s)


                ExampleNum = 0
                print( 'Epoch :', Epoch)
  
                EpochStart = time.time()
                for x, y in disTrain:
                   if len(x) < self.gpu_num:
                      continue
                   ExampleNum+=len(x)
                   yield x, y, Epoch
                TimeCost = time.time() - EpochStart
                
                Epoch +=1
            print('Seen ', ExampleNum, ' examples for text_discriminator. Time Cost : ', TimeCost)
  
      train_it = train_iter()
      drop_prob = 1.0
  
      TrainStart = time.time()
      epoch = 0
      uidx = 0
      HourIdx = 0
      print('train begin')
      while epoch < max_epoch:
        if time.time() - TrainStart >= 3600 * HourIdx:
                print('------------------------------------------Hour %d --------------------' % HourIdx)
                HourIdx +=1

        BatchStart = time.time()
        x, y, epoch  = next(train_it)
        uidx +=1
        #print('uidx is ', uidx)
        #print(len(x))
        if not len(x) % self.gpu_num == 0 or x is None:
           print('the positive data is bad')
           continue
        x_data_list = numpy.split(numpy.array(x), self.gpu_num)
        y_data_list = numpy.split(numpy.array(y), self.gpu_num)
  
        myFeed_dict={}
        for i, x, y in zip(range(self.gpu_num), x_data_list, y_data_list):
           x = x.tolist()
           x, y = dis_length_prepare(x, y, self.num_classes, self.max_len)
           myFeed_dict[self.x_list[i]]=x
           myFeed_dict[self.y_list[i]]=y
           myFeed_dict[self.drop_list[i]]=drop_prob
         
        _, loss_out, accuracy_out, grads_out = self.sess.run([self.train_optm, self.train_loss, self.train_accuracy, self.train_grads_and_vars], feed_dict=myFeed_dict)

        if uidx == 1:
            _ = self.sess.run(self.clip_ops)
            #x_variable = [self.sess.run(tf.assign(x, tf.clip_by_value(x, -1.0, 1.0))) for x in tf.trainable_variables() if self.scope in x.name] # clip the value into -0.01 to 0.01
        
        #print('ypred_for_auc is ', ypred_out)
        BatchTime = time.time()-BatchStart
        
        if numpy.mod(uidx, self.dispFreq) == 0:
            print("epoch %d, samples %d, loss %f, accuracy %f BatchTime %f, for discriminator pretraining " % (epoch, uidx * self.gpu_num * self.batch_size, loss_out, accuracy_out, BatchTime))

        if numpy.mod(uidx, self.saveFreq) == 0:
           print('save params when epoch %d, samples %d' %(epoch, uidx * self.gpu_num * self.batch_size))
           self.saver.save(self.sess, self.saveto)

        if numpy.mod(uidx, self.devFreq) == 0:
            print('doing nothing  on the evaluation sets')

