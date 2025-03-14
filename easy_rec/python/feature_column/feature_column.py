# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import logging
import collections

import tensorflow as tf

from easy_rec.python.builders import hyperparams_builder
from easy_rec.python.compat.feature_column import sequence_feature_column
from easy_rec.python.protos.feature_config_pb2 import FeatureConfig
from easy_rec.python.protos.feature_config_pb2 import WideOrDeep

from easy_rec.python.compat.feature_column import feature_column_v2 as feature_column  # NOQA

if tf.__version__ >= '2.0':
  min_max_variable_partitioner = tf.compat.v1.min_max_variable_partitioner
  tf = tf.compat.v1
else:
  min_max_variable_partitioner = tf.min_max_variable_partitioner


class FeatureKeyError(KeyError):

  def __init__(self, feature_name):
    super(FeatureKeyError, self).__init__(feature_name)


class SharedEmbedding(object):
   def __init__(self, embedding_name, index, sequence_combiner=None):
     self.embedding_name = embedding_name
     self.index = index
     self.sequence_combiner = sequence_combiner


class FeatureColumnParser(object):
  """Parse and generate feature columns."""

  def __init__(self,
               feature_configs,
               wide_deep_dict={},
               wide_output_dim=-1,
               use_embedding_variable=False):
    """Initializes a `FeatureColumnParser`.

    Args:
      feature_configs: collections of
            easy_rec.python.protos.feature_config_pb2.FeatureConfig
            or easy_rec.python.protos.feature_config_pb2.FeatureConfigV2.features
      wide_deep_dict: dict of {feature_name:WideOrDeep}, passed by
        easy_rec.python.layers.input_layer.InputLayer, it is defined in
        easy_rec.python.protos.easy_rec_model_pb2.EasyRecModel.feature_groups
      wide_output_dim: output dimension for wide columns
      use_embedding_variable: use EmbeddingVariable, which is provided by pai-tf
    """
    self._feature_configs = feature_configs
    self._wide_output_dim = wide_output_dim
    self._wide_deep_dict = wide_deep_dict
    self._deep_columns = {}
    self._wide_columns = {}
    self._sequence_columns = {}

    self._share_embed_names = {}
    self._share_embed_infos = {}

    self._use_embedding_variable = use_embedding_variable
    self._vocab_size = {}

    for config in self._feature_configs:
      if not config.HasField('embedding_name'):
        continue
      embed_name = config.embedding_name
      embed_info = {
          'embedding_dim':
              config.embedding_dim,
          'combiner':
              config.combiner,
          'initializer':
              config.initializer if config.HasField('initializer') else None,
          'max_partitions':
              config.max_partitions
      }
      if embed_name in self._share_embed_names:
        assert embed_info == self._share_embed_infos[embed_name], \
            'shared embed info of [%s] is not matched [%s] vs [%s]' % (
                embed_name, embed_info, self._share_embed_infos[embed_name])
        self._share_embed_names[embed_name] += 1
      else:
        self._share_embed_names[embed_name] = 1
        self._share_embed_infos[embed_name] = embed_info

    # remove not shared embedding names
    not_shared = [
        x for x in self._share_embed_names if self._share_embed_names[x] == 1
    ]
    for embed_name in not_shared:
      del self._share_embed_names[embed_name]
      del self._share_embed_infos[embed_name]

    logging.info('shared embeddings[num=%d]' % len(self._share_embed_names))
    for embed_name in self._share_embed_names:
      logging.info('\t%s: share_num[%d], share_info[%s]' %
                   (embed_name, self._share_embed_names[embed_name],
                    self._share_embed_infos[embed_name]))
    self._deep_share_embed_columns = {
        embed_name: [] for embed_name in self._share_embed_names
    }
    self._wide_share_embed_columns = {
        embed_name: [] for embed_name in self._share_embed_names
    }

    for config in self._feature_configs:
      assert isinstance(config, FeatureConfig)
      try:
        if config.feature_type == config.IdFeature:
          self.parse_id_feature(config)
        elif config.feature_type == config.TagFeature:
          self.parse_tag_feature(config)
        elif config.feature_type == config.RawFeature:
          self.parse_raw_feature(config)
        elif config.feature_type == config.ComboFeature:
          self.parse_combo_feature(config)
        elif config.feature_type == config.LookupFeature:
          self.parse_lookup_feature(config)
        elif config.feature_type == config.SequenceFeature:
          self.parse_sequence_feature(config)
        elif config.feature_type == config.ExprFeature:
          self.parse_expr_feature(config)
        else:
          assert False, 'invalid feature type: %s' % config.feature_type
      except FeatureKeyError:
        pass

    for embed_name in self._share_embed_names:
      initializer = None
      if self._share_embed_infos[embed_name]['initializer']:
        initializer = hyperparams_builder.build_initializer(
            self._share_embed_infos[embed_name]['initializer'])
      partitioner = self._build_partitioner(
          self._share_embed_infos[embed_name]['max_partitions'])
      # for handling share embedding columns
      share_embed_fcs = feature_column.shared_embedding_columns(
          self._deep_share_embed_columns[embed_name],
          self._share_embed_infos[embed_name]['embedding_dim'],
          initializer=initializer,
          shared_embedding_collection_name=embed_name,
          combiner=self._share_embed_infos[embed_name]['combiner'],
          partitioner=partitioner,
          use_embedding_variable=self._use_embedding_variable)
      self._deep_share_embed_columns[embed_name] = share_embed_fcs
      # for handling wide share embedding columns
      if len(self._wide_share_embed_columns[embed_name]) == 0:
        continue
      share_embed_fcs = feature_column.shared_embedding_columns(
          self._wide_share_embed_columns[embed_name],
          self._wide_output_dim,
          initializer=initializer,
          shared_embedding_collection_name=embed_name + '_wide',
          combiner='sum',
          partitioner=partitioner,
          use_embedding_variable=self._use_embedding_variable)
      self._wide_share_embed_columns[embed_name] = share_embed_fcs

    for fc_name in self._deep_columns:
      fc = self._deep_columns[fc_name]
      if isinstance(fc, SharedEmbedding):
        self._deep_columns[fc_name] = self._get_shared_embedding_column(fc)

    for fc_name in self._wide_columns:
      fc = self._wide_columns[fc_name]
      if isinstance(fc, SharedEmbedding):
        self._wide_columns[fc_name] = self._get_shared_embedding_column(
            fc, deep=False)

    for fc_name in self._sequence_columns:
      fc = self._sequence_columns[fc_name]
      if isinstance(fc, SharedEmbedding):
        self._sequence_columns[fc_name] = self._get_shared_embedding_column(fc)

  @property
  def wide_columns(self):
    return self._wide_columns

  @property
  def deep_columns(self):
    return self._deep_columns

  @property
  def sequence_columns(self):
    return self._sequence_columns

  def is_wide(self, config):
    if config.HasField('feature_name'):
      feature_name = config.feature_name
    else:
      feature_name = config.input_names[0]
    if feature_name not in self._wide_deep_dict:
      raise FeatureKeyError(feature_name)
    return self._wide_deep_dict[feature_name] in [
        WideOrDeep.WIDE, WideOrDeep.WIDE_AND_DEEP
    ]

  def is_deep(self, config):
    if config.HasField('feature_name'):
      feature_name = config.feature_name
    else:
      feature_name = config.input_names[0]
    # DEEP or WIDE_AND_DEEP
    if feature_name not in self._wide_deep_dict:
      raise FeatureKeyError(feature_name)
    return self._wide_deep_dict[feature_name] in [
        WideOrDeep.DEEP, WideOrDeep.WIDE_AND_DEEP
    ]

  def _get_vocab_size(self, vocab_path):
    if vocab_path in self._vocab_size:
      return self._vocab_size[vocab_path]
    with tf.gfile.GFile(vocab_path, 'r') as fin:
      vocabulary_size = sum(1 for _ in fin)
      self._vocab_size[vocab_path] = vocabulary_size
      return vocabulary_size

  def parse_id_feature(self, config):
    """Generate id feature columns.

    if hash_bucket_size or vocab_list or vocab_file is set,
    then will accept input tensor of string type, otherwise will accept input
    tensor of integer type.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    hash_bucket_size = config.hash_bucket_size
    if hash_bucket_size > 0:
      fc = feature_column.categorical_column_with_hash_bucket(
          config.input_names[0], hash_bucket_size=hash_bucket_size)
    elif config.vocab_list:
      fc = feature_column.categorical_column_with_vocabulary_list(
          config.input_names[0],
          default_value=0,
          vocabulary_list=config.vocab_list)
    elif config.vocab_file:
      fc = feature_column.categorical_column_with_vocabulary_file(
          config.input_names[0],
          default_value=0,
          vocabulary_file=config.vocab_file,
          vocabulary_size=self._get_vocab_size(config.vocab_file))
    else:
      fc = feature_column.categorical_column_with_identity(
          config.input_names[0], config.num_buckets, default_value=0)

    if self.is_wide(config):
      self._add_wide_embedding_column(fc, config)
    if self.is_deep(config):
      self._add_deep_embedding_column(fc, config)

  def parse_tag_feature(self, config):
    """Generate tag feature columns.

    if hash_bucket_size is set, will accept input of SparseTensor of string,
    otherwise num_buckets must be set, will accept input of SparseTensor of integer.
    tag feature preprocess is done in easy_rec/python/input/input.py: Input. _preprocess

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    hash_bucket_size = config.hash_bucket_size
    if config.HasField('hash_bucket_size'):
      tag_fc = feature_column.categorical_column_with_hash_bucket(
          config.input_names[0], hash_bucket_size, dtype=tf.string)
    elif config.vocab_list:
      tag_fc = feature_column.categorical_column_with_vocabulary_list(
          config.input_names[0],
          default_value=0,
          vocabulary_list=config.vocab_list)
    elif config.vocab_file:
      tag_fc = feature_column.categorical_column_with_vocabulary_file(
          config.input_names[0],
          default_value=0,
          vocabulary_file=config.vocab_file,
          vocabulary_size=self._get_vocab_size(config.vocab_file))
    else:
      tag_fc = feature_column.categorical_column_with_identity(
          config.input_names[0], config.num_buckets, default_value=0)

    if len(config.input_names) > 1:
      tag_fc = feature_column.weighted_categorical_column(
          tag_fc, weight_feature_key=config.input_names[1], dtype=tf.float32)
    elif config.HasField('kv_separator'):
      wgt_name = config.input_names[0] + '_WEIGHT'
      tag_fc = feature_column.weighted_categorical_column(
          tag_fc, weight_feature_key=wgt_name, dtype=tf.float32)

    if self.is_wide(config):
      self._add_wide_embedding_column(tag_fc, config)
    if self.is_deep(config):
      self._add_deep_embedding_column(tag_fc, config)

  def parse_raw_feature(self, config):
    """Generate raw features columns.

    if boundaries is set, will be converted to category_column first.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    fc = feature_column.numeric_column(
        config.input_names[0], shape=(config.raw_input_dim,))

    bounds = None
    if config.boundaries:
      bounds = list(config.boundaries)
      bounds.sort()
    elif config.num_buckets > 1 and config.max_val > config.min_val:
      # the feature values are already normalized into [0, 1]
      bounds = [
          x / float(config.num_buckets) for x in range(0, config.num_buckets)
      ]
      logging.info('discrete %s into %d buckets' %
                   (feature_name, config.num_buckets))

    if bounds:
      try:
        fc = feature_column.bucketized_column(fc, bounds)
      except Exception as e:
        tf.logging.error('bucketized_column [%s] with bounds %s error' %
                         (fc.name, str(bounds)))
        raise e
      if self.is_wide(config):
        self._add_wide_embedding_column(fc, config)
      if self.is_deep(config):
        self._add_deep_embedding_column(fc, config)
    else:
      tmp_id_col = feature_column.categorical_column_with_identity(
          config.input_names[0] + '_raw_proj_id',
          config.raw_input_dim,
          default_value=0)
      wgt_fc = feature_column.weighted_categorical_column(
          tmp_id_col,
          weight_feature_key=config.input_names[0] + '_raw_proj_val',
          dtype=tf.float32)
      if self.is_wide(config):
        self._add_wide_embedding_column(wgt_fc, config)
      if self.is_deep(config):
        if config.embedding_dim > 0:
          self._add_deep_embedding_column(wgt_fc, config)
        else:
          self._deep_columns[feature_name] = fc

  def parse_expr_feature(self, config):
    """Generate raw features columns.

    if boundaries is set, will be converted to category_column first.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    fc = feature_column.numeric_column(
        feature_name, shape=(1,))
    if self.is_wide(config):
        self._add_wide_embedding_column(fc, config)
    if self.is_deep(config):
        self._deep_columns[feature_name] = fc


  def parse_combo_feature(self, config):
    """Generate combo feature columns.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    assert len(config.input_names) >= 2
    fc = feature_column.crossed_column(
        config.input_names, config.hash_bucket_size, hash_key=None)

    if self.is_wide(config):
      self._add_wide_embedding_column(fc, config)
    if self.is_deep(config):
      self._add_deep_embedding_column(fc, config)

  def parse_lookup_feature(self, config):
    """Generate lookup feature columns.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    assert config.HasField('hash_bucket_size')
    hash_bucket_size = config.hash_bucket_size
    fc = feature_column.categorical_column_with_hash_bucket(
        feature_name, hash_bucket_size, dtype=tf.string)

    if self.is_wide(config):
      self._add_wide_embedding_column(fc, config)
    if self.is_deep(config):
      self._add_deep_embedding_column(fc, config)

  def parse_sequence_feature(self, config):
    """Generate sequence feature columns.

    Args:
      config: instance of easy_rec.python.protos.feature_config_pb2.FeatureConfig
    """
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    sub_feature_type = config.sub_feature_type
    assert sub_feature_type in [config.IdFeature, config.RawFeature], \
        'Current sub_feature_type only support IdFeature and RawFeature.'
    if sub_feature_type == config.IdFeature:
      if config.HasField('hash_bucket_size'):
        hash_bucket_size = config.hash_bucket_size
        fc = sequence_feature_column.sequence_categorical_column_with_hash_bucket(
            config.input_names[0], hash_bucket_size, dtype=tf.string)
      elif config.vocab_list:
        fc = sequence_feature_column.sequence_categorical_column_with_vocabulary_list(
            config.input_names[0],
            default_value=0,
            vocabulary_list=config.vocab_list)
      elif config.vocab_file:
        fc = sequence_feature_column.sequence_categorical_column_with_vocabulary_file(
            config.input_names[0],
            default_value=0,
            vocabulary_file=config.vocab_file,
            vocabulary_size=self._get_vocab_size(config.vocab_file))
      else:
        fc = sequence_feature_column.sequence_categorical_column_with_identity(
            config.input_names[0], config.num_buckets, default_value=0)
    else:
      bounds = None
      fc = sequence_feature_column.sequence_numeric_column(
          config.input_names[0], shape=(1,))
      if config.hash_bucket_size > 0:
        hash_bucket_size = config.hash_bucket_size
        assert sub_feature_type == config.IdFeature, \
            'You should set sub_feature_type to IdFeature to use hash_bucket_size.'
      elif config.boundaries:
        bounds = list(config.boundaries)
        bounds.sort()
      elif config.num_buckets > 1 and config.max_val > config.min_val:
        # the feature values are already normalized into [0, 1]
        bounds = [
            x / float(config.num_buckets) for x in range(0, config.num_buckets)
        ]
        logging.info('sequence feature discrete %s into %d buckets' %
                     (feature_name, config.num_buckets))
      if bounds:
        try:
          fc = sequence_feature_column.sequence_numeric_column_with_bucketized_column(
              fc, bounds)
        except Exception as e:
          tf.logging.error(
              'sequence features bucketized_column [%s] with bounds %s error' %
              (config.input_names[0], str(bounds)))
          raise e
      elif config.hash_bucket_size <= 0:
        if config.embedding_dim > 0:
          tmp_id_col = sequence_feature_column.sequence_categorical_column_with_identity(
              config.input_names[0] + '_raw_proj_id',
              config.raw_input_dim,
              default_value=0)
          wgt_fc = sequence_feature_column.sequence_weighted_categorical_column(
              tmp_id_col,
              weight_feature_key=config.input_names[0] + '_raw_proj_val',
              dtype=tf.float32)
          fc = wgt_fc
        else:
          fc = sequence_feature_column.sequence_numeric_column_with_raw_column(
              fc, config.sequence_length)

    if config.embedding_dim > 0:
      self._add_deep_embedding_column(fc, config)
    else:
      self._sequence_columns[feature_name] = fc

  def _build_partitioner(self, max_partitions):
    if max_partitions > 1:
      if self._use_embedding_variable:
        # pai embedding_variable should use fixed_size_partitioner
        return tf.fixed_size_partitioner(num_shards=max_partitions)
      else:
        return min_max_variable_partitioner(max_partitions=max_partitions)
    else:
      return None

  def _add_shared_embedding_column(self, embedding_name, fc, deep=True):
    curr_id = len(self._deep_share_embed_columns[embedding_name])
    if deep:
      self._deep_share_embed_columns[embedding_name].append(fc)
    else:
      self._wide_share_embed_columns[embedding_name].append(fc)
    return SharedEmbedding(embedding_name, curr_id, None)

  def _get_shared_embedding_column(self, fc_handle, deep=True):
    embed_name, embed_id = fc_handle.embedding_name, fc_handle.index 
    if deep:
      tmp = self._deep_share_embed_columns[embed_name][embed_id]
    else:
      tmp = self._wide_share_embed_columns[embed_name][embed_id]
    tmp.sequence_combiner = fc_handle.sequence_combiner
    return tmp

  def _add_wide_embedding_column(self, fc, config):
    """Generate wide feature columns.

    We use embedding to simulate wide column, which is more efficient than indicator column for
    sparse features
    """
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    assert self._wide_output_dim > 0, 'wide_output_dim is not set'
    if config.embedding_name in self._wide_share_embed_columns:
      wide_fc = self._add_shared_embedding_column(
          config.embedding_name, fc, deep=False)
    else:
      initializer = None
      if config.HasField('initializer'):
        initializer = hyperparams_builder.build_initializer(config.initializer)
      wide_fc = feature_column.embedding_column(
          fc,
          self._wide_output_dim,
          combiner='sum',
          initializer=initializer,
          partitioner=self._build_partitioner(config.max_partitions),
          use_embedding_variable=self._use_embedding_variable)
    self._wide_columns[feature_name] = wide_fc

  def _add_deep_embedding_column(self, fc, config):
    """Generate deep feature columns."""
    feature_name = config.feature_name if config.HasField('feature_name') \
        else config.input_names[0]
    assert config.embedding_dim > 0, 'embedding_dim is not set for %s' % feature_name
    if config.embedding_name in self._deep_share_embed_columns:
      fc = self._add_shared_embedding_column(config.embedding_name, fc)
    else:
      initializer = None
      if config.HasField('initializer'):
        initializer = hyperparams_builder.build_initializer(config.initializer)
      fc = feature_column.embedding_column(
          fc,
          config.embedding_dim,
          combiner=config.combiner,
          initializer=initializer,
          partitioner=self._build_partitioner(config.max_partitions),
          use_embedding_variable=self._use_embedding_variable)
    if config.feature_type != config.SequenceFeature:
      self._deep_columns[feature_name] = fc
    else:
      if config.HasField('sequence_combiner'):
        fc.sequence_combiner = config.sequence_combiner
      self._sequence_columns[feature_name] = fc
