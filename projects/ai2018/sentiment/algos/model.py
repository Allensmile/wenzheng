#!/usr/bin/env python 
# -*- coding: utf-8 -*-
# ==============================================================================
#          \file   ptr-net.py
#        \author   chenghuige  
#          \date   2018-01-15 11:50:08.306272
#   \Description  
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys 
import os

import tensorflow as tf  
flags = tf.app.flags
FLAGS = flags.FLAGS

from tensorflow import keras

import wenzheng
from wenzheng.utils import vocabulary, embedding

from algos.config import NUM_CLASSES, NUM_ATTRIBUTES
from algos.weights import *

import melt
logging = melt.logging
import numpy as np

class Model(melt.Model):
  def __init__(self):
    super(Model, self).__init__()
    vocabulary.init()
    vocab_size = vocabulary.get_vocab_size() 
    logging.info('vocab_size:', vocab_size)

    self.num_layers = FLAGS.num_layers
    self.num_units = FLAGS.rnn_hidden_size
    self.keep_prob = FLAGS.keep_prob

    logging.info('num_layers:', self.num_layers)
    logging.info('num_unints:', self.num_units)
    logging.info('keep_prob:', self.keep_prob)

    ## adadelta adagrad will need cpu, so just use adam..
    #with tf.device('/cpu:0'):
    self.embedding = wenzheng.utils.Embedding(vocab_size, FLAGS.emb_dim, 
                                              FLAGS.word_embedding_file, 
                                              trainable=FLAGS.finetune_word_embedding)

    if FLAGS.use_label_emb or FLAGS.use_label_att:
      assert not FLAGS.use_label_emb and FLAGS.use_label_att
      self.label_embedding = melt.layers.Embedding(NUM_CLASSES * NUM_ATTRIBUTES, FLAGS.emb_dim)
      if not FLAGS.use_label_att:
        self.label_dense = keras.layers.Dense(FLAGS.emb_dim, activation=tf.nn.relu)
      else:
        self.att_dot_attention = melt.layers.DotAttention(hidden=self.num_units, keep_prob=self.keep_prob, combiner=FLAGS.att_combiner)
        self.att_encode = melt.layers.CudnnRnn(num_layers=1, num_units=self.num_units, keep_prob=self.keep_prob)

    self.encode = melt.layers.CudnnRnn(num_layers=self.num_layers, num_units=self.num_units, keep_prob=self.keep_prob)

    # hier a bit worse
    self.hier_encode = melt.layers.HierEncode() if FLAGS.use_hier_encode else None
    
    # top-k best, max,att can benfit ensemble(better then max, worse then topk-3), topk,att now best with 2layers
    logging.info('encoder_output_method:', FLAGS.encoder_output_method)
    logging.info('topk:', FLAGS.top_k)
    self.pooling = melt.layers.Pooling(FLAGS.encoder_output_method, top_k=FLAGS.top_k)
    #self.pooling = keras.layers.GlobalMaxPool1D()

    # mlp not help much!
    if FLAGS.mlp_ratio != 0:
      self.dropout = keras.layers.Dropout(0.3)
      if FLAGS.mlp_ratio < 0:
        # here activation hurt perf!
        #self.dense = keras.layers.Dense(NUM_ATTRIBUTES * NUM_CLASSES * 2, activation=tf.nn.relu)
        self.dense = keras.layers.Dense(NUM_ATTRIBUTES * NUM_CLASSES * 2)
      elif FLAGS.mlp_ratio <= 1:
        self.dense = melt.layers.DynamicDense(FLAGS.mlp_ratio)
      else:
        self.dense = kears.layers.Dense(int(FLAGS.mlp_ratio))
    else:
      self.dense = None

    self.logits = keras.layers.Dense(NUM_ATTRIBUTES * NUM_CLASSES, activation=None)
    
  def call(self, input, training=False):
    x = input['content'] 

    batch_size = melt.get_shape(x, 0)
    length = melt.length(x)
    #with tf.device('/cpu:0'):
    x = self.embedding(x)

    num_units = [melt.get_shape(x, -1) if layer == 0 else 2 * self.num_units for layer in range(self.num_layers)]
    #print('----------------length', tf.reduce_max(length), inputs.comment.shape)
    mask_fws = [melt.dropout(tf.ones([batch_size, 1, num_units[layer]], dtype=tf.float32), keep_prob=self.keep_prob, training=training, mode=None) for layer in range(self.num_layers)]
    mask_bws = [melt.dropout(tf.ones([batch_size, 1, num_units[layer]], dtype=tf.float32), keep_prob=self.keep_prob, training=training, mode=None) for layer in range(self.num_layers)]
    x = self.encode(x, length, mask_fws=mask_fws, mask_bws=mask_bws)
    #x = self.encode(x)

    # not help
    if self.hier_encode is not None:
      x = self.hier_encode(x, length)

    if FLAGS.use_label_att:
      label_emb = self.label_embedding(None)
      label_seq = tf.tile(tf.expand_dims(label_emb, 0), [batch_size, 1, 1])
      lc_att = self.att_dot_attention(x, label_seq, mask=tf.ones([batch_size, NUM_ATTRIBUTES * NUM_CLASSES], tf.bool), training=training)

      num_units = [melt.get_shape(lc_att, -1) if layer == 0 else 2 * self.num_units for layer in range(self.num_layers)]
      mask_fws = [melt.dropout(tf.ones([batch_size, 1, num_units[layer]], dtype=tf.float32), keep_prob=self.keep_prob, training=training, mode=None) for layer in range(1)]
      mask_bws = [melt.dropout(tf.ones([batch_size, 1, num_units[layer]], dtype=tf.float32), keep_prob=self.keep_prob, training=training, mode=None) for layer in range(1)]
      x = self.att_encode(lc_att, length, mask_fws=mask_fws, mask_bws=mask_bws)
  
    x = self.pooling(x, length, calc_word_scores=self.debug)
    #x = self.pooling(x)

    # not help much
    if self.dense is not None:
      x = self.dense(x)
      x = self.dropout(x)

    if not FLAGS.use_label_emb:
      x = self.logits(x)
    else:
      x = self.label_dense(x)
      # TODO..
      x = melt.dot(x, self.label_embedding(None))

    # # No help match
    # if training and FLAGS.num_learning_rate_weights == NUM_ATTRIBUTES * NUM_CLASSES:
    #   x = melt.adjust_lrs(x)

    x = tf.reshape(x, [batch_size, NUM_ATTRIBUTES, NUM_CLASSES])
    
    return x
