# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import json
import logging
import os

import common_io
import tensorflow as tf

from easy_rec.python.utils import config_util

if tf.__version__ >= '2.0':
  tf = tf.compat.v1

logging.basicConfig(
    format='[%(levelname)s] %(asctime)s %(filename)s:%(lineno)d : %(message)s',
    level=logging.INFO)
tf.app.flags.DEFINE_string('template_config_path', None,
                           'Path to template pipeline config '
                           'file.')
tf.app.flags.DEFINE_string('output_config_path', None,
                           'Path to output pipeline config '
                           'file.')
tf.app.flags.DEFINE_string('config_table', '', 'config table')

FLAGS = tf.app.flags.FLAGS


def main(argv):
  pipeline_config = config_util.get_configs_from_pipeline_file(
      FLAGS.template_config_path)

  reader = common_io.table.TableReader(
      FLAGS.config_table, selected_cols='feature,feature_info')
  feature_info_map = {}
  while True:
    try:
      record = reader.read()
      feature_name = record[0][0]
      feature_info_map[feature_name] = json.loads(record[0][1])
    except common_io.exception.OutOfRangeException:
      reader.close()
      break

  for feature_config in config_util.get_compatible_feature_configs(
      pipeline_config):
    feature_name = feature_config.input_names[0]
    if feature_name in feature_info_map:
      logging.info('edited %s' % feature_name)
      feature_config.embedding_dim = int(
          feature_info_map[feature_name]['embedding_dim'])
      logging.info('modify embedding_dim to %s' % feature_config.embedding_dim)
      if 'boundary' in feature_info_map[feature_name]:
        feature_config.ClearField('boundaries')
        feature_config.boundaries.extend(
            [float(i) for i in feature_info_map[feature_name]['boundary']])
        logging.info('modify boundaries to %s' % feature_config.boundaries)
      elif 'hash_bucket_size' in feature_info_map[feature_name]:
        feature_config.hash_bucket_size = int(
            feature_info_map[feature_name]['hash_bucket_size'])
        logging.info('modify hash_bucket_size to %s' %
                     feature_config.hash_bucket_size)
  # modify num_steps
  pipeline_config.train_config.num_steps = feature_info_map['__NUM_STEPS__'][
      'num_steps']
  logging.info('modify num_steps to %s' %
               pipeline_config.train_config.num_steps)
  # modify decay_steps
  optimizer_configs = pipeline_config.train_config.optimizer_config
  for optimizer_config in optimizer_configs:
    optimizer = optimizer_config.WhichOneof('optimizer')
    optimizer = getattr(optimizer_config, optimizer)
    learning_rate = optimizer.learning_rate.WhichOneof('learning_rate')
    learning_rate = getattr(optimizer.learning_rate, learning_rate)
    if hasattr(learning_rate, 'decay_steps'):
      learning_rate.decay_steps = feature_info_map['__DECAY_STEPS__'][
          'decay_steps']
    logging.info('modify decay_steps to %s' % learning_rate.decay_steps)

  config_dir, config_name = os.path.split(FLAGS.output_config_path)
  config_util.save_pipeline_config(pipeline_config, config_dir, config_name)


if __name__ == '__main__':
  tf.app.run()

