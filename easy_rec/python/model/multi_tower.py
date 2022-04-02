# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.

import tensorflow as tf

from easy_rec.python.layers import dnn
from easy_rec.python.model.rank_model import RankModel
from easy_rec.python.protos.loss_pb2 import LossType
from easy_rec.python.loss.pairwise_loss import pairwise_loss
from easy_rec.python.protos.multi_tower_pb2 import MultiTower as MultiTowerConfig  # NOQA

if tf.__version__ >= '2.0':
  tf = tf.compat.v1


class MultiTower(RankModel):

  def __init__(self,
               model_config,
               feature_configs,
               features,
               labels=None,
               is_training=False):
    super(MultiTower, self).__init__(model_config, feature_configs, features,
                                     labels, is_training)
    self._losses = self._model_config.losses
    print("loss num:", len(self._losses))
    assert self._model_config.WhichOneof('model') == 'multi_tower', \
        'invalid model config: %s' % self._model_config.WhichOneof('model')
    self._model_config = self._model_config.multi_tower
    assert isinstance(self._model_config, MultiTowerConfig)

    self._tower_features = []
    self._tower_num = len(self._model_config.towers)
    for tower_id in range(self._tower_num):
      tower = self._model_config.towers[tower_id]
      tower_feature, _ = self._input_layer(self._feature_dict, tower.input)
      self._tower_features.append(tower_feature)

  def build_predict_graph(self):
    tower_fea_arr = []
    for tower_id in range(self._tower_num):
      tower_fea = self._tower_features[tower_id]
      tower = self._model_config.towers[tower_id]
      tower_name = tower.input
      tower_fea = tf.layers.batch_normalization(
          tower_fea,
          training=self._is_training,
          trainable=True,
          name='%s_fea_bn' % tower_name)

      tower_dnn_layer = dnn.DNN(tower.dnn, self._l2_reg, '%s_dnn' % tower_name,
                                self._is_training)
      tower_fea = tower_dnn_layer(tower_fea)
      tower_fea_arr.append(tower_fea)

    all_fea = tf.concat(tower_fea_arr, axis=1)
    final_dnn_layer = dnn.DNN(self._model_config.final_dnn, self._l2_reg,
                              'final_dnn', self._is_training)
    all_fea = final_dnn_layer(all_fea)
    output = tf.layers.dense(all_fea, self._num_class, name='output')

    self._add_to_prediction_dict(output)

    return self._prediction_dict

  def build_loss_graph(self):
    if len(self._losses) == 0:
      return super.build_loss_graph()

    logits = self._prediction_dict['logits']
    labels = self._labels[self._label_name]
    for loss in self._losses:
      if loss.loss_type == LossType.PAIR_WISE_LOSS:
        loss_value = pairwise_loss(labels, logits)
        self._loss_dict['pairwise_loss'] = loss_value * loss.weight
        print("add pairwise loss")
      elif loss.loss_type == LossType.CLASSIFICATION:
        loss_value = tf.losses.sigmoid_cross_entropy(labels, logits,
                                                     self._sample_weight)
        self._loss_dict['sigmoid_loss'] = loss_value * loss.weight
        print("add sigmoid loss")
    return self._loss_dict
