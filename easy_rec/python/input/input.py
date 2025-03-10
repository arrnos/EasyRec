# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import logging
from abc import abstractmethod
from collections import OrderedDict

import six
import tensorflow as tf

from easy_rec.python.core import sampler as sampler_lib
from easy_rec.python.protos.dataset_pb2 import DatasetConfig
from easy_rec.python.utils import config_util
from easy_rec.python.utils import constant
from easy_rec.python.utils.expr_util import get_expression
from easy_rec.python.utils.input_utils import get_type_defaults
from easy_rec.python.utils.load_class import get_register_class_meta

if tf.__version__ >= '2.0':
  tf = tf.compat.v1

_INPUT_CLASS_MAP = {}
_meta_type = get_register_class_meta(_INPUT_CLASS_MAP, have_abstract_class=True)


class Input(six.with_metaclass(_meta_type, object)):

  def __init__(self,
               data_config,
               feature_configs,
               input_path,
               task_index=0,
               task_num=1):
    self._data_config = data_config

    # tf.estimator.ModeKeys.*, only available before
    # calling self._build
    self._mode = None

    if self._data_config.auto_expand_input_fields:
      input_fields = [x for x in self._data_config.input_fields]
      while len(self._data_config.input_fields) > 0:
        self._data_config.input_fields.pop()
      for field in input_fields:
        tmp_names = config_util.auto_expand_names(field.input_name)
        for tmp_name in tmp_names:
          one_field = DatasetConfig.Field()
          one_field.CopyFrom(field)
          one_field.input_name = tmp_name
          self._data_config.input_fields.append(one_field)

    self._input_fields = [x.input_name for x in data_config.input_fields]
    self._input_dims = [x.input_dim for x in data_config.input_fields]
    self._input_field_types = [x.input_type for x in data_config.input_fields]
    self._input_field_defaults = [
        x.default_val for x in data_config.input_fields
    ]
    self._label_fields = list(data_config.label_fields)
    self._label_sep = list(data_config.label_sep)
    self._label_dim = list(data_config.label_dim)
    if len(self._label_dim) < len(self._label_fields):
      for x in range(len(self._label_fields) - len(self._label_dim)):
        self._label_dim.append(1)

    self._batch_size = data_config.batch_size
    self._prefetch_size = data_config.prefetch_size
    self._feature_configs = list(feature_configs)
    self._task_index = task_index
    self._task_num = task_num

    self._input_path = input_path

    # findout effective fields
    self._effective_fields = []

    # for multi value inputs, the types maybe different
    # from the types defined in input_fields
    # it is used in create_multi_placeholders
    self._multi_value_types = {}

    for fc in self._feature_configs:
      for input_name in fc.input_names:
        assert input_name in self._input_fields, 'invalid input_name in %s' % str(
            fc)
        if input_name not in self._effective_fields:
          self._effective_fields.append(input_name)

      if fc.feature_type in [fc.TagFeature, fc.SequenceFeature]:
        if fc.hash_bucket_size > 0:
          self._multi_value_types[fc.input_names[0]] = tf.string
        else:
          self._multi_value_types[fc.input_names[0]] = tf.int64
        if len(fc.input_names) > 1:
          self._multi_value_types[fc.input_names[1]] = tf.float32

      if fc.feature_type == fc.RawFeature:
        self._multi_value_types[fc.input_names[0]] = tf.float32

    # add sample weight to effective fields
    if self._data_config.HasField('sample_weight'):
      self._effective_fields.append(self._data_config.sample_weight)

    self._effective_fids = [
        self._input_fields.index(x) for x in self._effective_fields
    ]
    # sort fids from small to large
    self._effective_fids = list(set(self._effective_fids))
    self._effective_fields = [
        self._input_fields[x] for x in self._effective_fids
    ]

    self._label_fids = [self._input_fields.index(x) for x in self._label_fields]

    # virtual fields generated by self._preprocess
    # which will be inputs to feature columns
    self._appended_fields = []

    # sampler
    self._sampler = None
    if input_path is not None:
      # build sampler only when train and eval
      self._sampler = sampler_lib.build(data_config)

    self.get_type_defaults = get_type_defaults

  @property
  def num_epochs(self):
    if self._data_config.num_epochs > 0:
      return self._data_config.num_epochs
    else:
      return None

  def get_tf_type(self, field_type):
    type_map = {
        DatasetConfig.INT32: tf.int32,
        DatasetConfig.INT64: tf.int64,
        DatasetConfig.STRING: tf.string,
        DatasetConfig.BOOL: tf.bool,
        DatasetConfig.FLOAT: tf.float32,
        DatasetConfig.DOUBLE: tf.double
    }
    assert field_type in type_map, 'invalid type: %s' % field_type
    return type_map[field_type]

  def create_multi_placeholders(self, export_config):
    """Create multiply placeholders on export, one for each feature.

    Args:
      export_config: ExportConfig instance.
    """
    self._mode = tf.estimator.ModeKeys.PREDICT

    if export_config.multi_value_fields:
      export_fields_name = export_config.multi_value_fields.input_name
    else:
      export_fields_name = None
    placeholder_named_by_input = export_config.placeholder_named_by_input

    sample_weight_field = ""
    if self._data_config.HasField('sample_weight'):
      sample_weight_field = self._data_config.sample_weight

    if export_config.filter_inputs:
      effective_fids = list(self._effective_fids)
    else:
      effective_fids = [
          fid for fid in range(len(self._input_fields))
          if self._input_fields[fid] not in self._label_fields  and self._input_fields[fid] != sample_weight_field
      ]

    inputs = {}
    for fid in effective_fids:
      input_name = self._input_fields[fid]
      if placeholder_named_by_input:
        placeholder_name = input_name
      else:
        placeholder_name = 'input_%d' % fid
      if input_name in export_fields_name:
        tf_type = self._multi_value_types[input_name]
        logging.info('multi value input_name: %s, dtype: %s' %
                     (input_name, tf_type))
        finput = tf.placeholder(tf_type, [None, None], name=placeholder_name)
      else:
        ftype = self._input_field_types[fid]
        tf_type = self.get_tf_type(ftype)
        logging.info('input_name: %s, dtype: %s' % (input_name, tf_type))
        finput = tf.placeholder(tf_type, [None], name=placeholder_name)
      inputs[input_name] = finput
    features = {x: inputs[x] for x in inputs}
    features = self._preprocess(features)
    return inputs, features

  def create_placeholders(self, export_config):
    self._mode = tf.estimator.ModeKeys.PREDICT
    inputs_placeholder = tf.placeholder(tf.string, [None], name='features')
    input_vals = tf.string_split(
        inputs_placeholder, self._data_config.separator,
        skip_empty=False).values

    sample_weight_field = ""
    if self._data_config.HasField('sample_weight'):
      sample_weight_field = self._data_config.sample_weight

    if export_config.filter_inputs:
      effective_fids = list(self._effective_fids)
      logging.info('number of effective inputs:%d, total number inputs: %d' %
                   (len(effective_fids), len(self._input_fields)))
    else:
      effective_fids = [
          fid for fid in range(len(self._input_fields))
          if self._input_fields[fid] not in self._label_fields and self._input_fields[fid] != sample_weight_field
      ]
      logging.info(
          'will not filter any input[except labels], total number inputs:%d' %
          len(effective_fids))
    input_vals = tf.reshape(
        input_vals, [-1, len(effective_fids)], name='input_reshape')
    features = {}
    for tmp_id, fid in enumerate(effective_fids):
      ftype = self._input_field_types[fid]
      tf_type = self.get_tf_type(ftype)
      input_name = self._input_fields[fid]
      if tf_type in [tf.float32, tf.double, tf.int32, tf.int64]:
        features[input_name] = tf.string_to_number(
            input_vals[:, tmp_id],
            tf_type,
            name='input_str_to_%s' % tf_type.name)
      else:
        if ftype not in [DatasetConfig.STRING]:
          logging.warning('unexpected field type: ftype=%s tf_type=%s' %
                          (ftype, tf_type))
        features[input_name] = input_vals[:, tmp_id]
    features = self._preprocess(features)
    return {'features': inputs_placeholder}, features

  def _get_features(self, fields):
    field_dict = {x: fields[x] for x in self._effective_fields if x in fields}
    for k in self._appended_fields:
      field_dict[k] = fields[k]
    if constant.SAMPLE_WEIGHT in fields:
      logging.info('will use field %s as sample weight' %
                   self._data_config.sample_weight)
      field_dict[constant.SAMPLE_WEIGHT] = fields[constant.SAMPLE_WEIGHT]
    return field_dict

  def _get_labels(self, fields):
    return OrderedDict([
        (x, tf.squeeze(fields[x], axis=1) if len(fields[x].get_shape()) == 2 and
         fields[x].get_shape()[1] == 1 else fields[x])
        for x in self._label_fields
    ])

  def _preprocess(self, field_dict):
    """Preprocess the feature columns.

    preprocess some feature columns, such as TagFeature or LookupFeature,
    it is expected to handle batch inputs and single input,
    it could be customized in subclasses

    Args:
      field_dict: string to tensor, tensors are dense,
          could be of shape [batch_size], [batch_size, None], or of shape []

    Returns:
      output_dict: some of the tensors are transformed into sparse tensors,
          such as input tensors of tag features and lookup features
    """
    parsed_dict = {}
    if self._sampler is not None:
      sampler_type = self._data_config.WhichOneof('sampler')
      sampler_config = getattr(self._data_config, sampler_type)
      item_ids = field_dict[sampler_config.item_id_field]
      if sampler_type == 'negative_sampler':
        sampled = self._sampler.get(item_ids)
      elif sampler_type == 'negative_sampler_v2':
        user_ids = field_dict[sampler_config.user_id_field]
        sampled = self._sampler.get(user_ids, item_ids)
      elif sampler_type.startswith('hard_negative_sampler'):
        user_ids = field_dict[sampler_config.user_id_field]
        sampled = self._sampler.get(user_ids, item_ids)
      else:
        raise ValueError('Unknown sampler %s' % sampler_type)
      for k, v in sampled.items():
        if k in field_dict:
          field_dict[k] = tf.concat([field_dict[k], v], axis=0)
        else:
          parsed_dict[k] = v
          self._appended_fields.append(k)

    print("[input] all feature names: {}".format([fc.feature_name for fc in self._feature_configs]))
    for fc in self._feature_configs:
      feature_name = fc.feature_name
      feature_type = fc.feature_type
      input_0 = fc.input_names[0]
      if feature_type == fc.TagFeature:
        input_0 = fc.input_names[0]
        field = field_dict[input_0]
        # Construct the output of TagFeature according to the dimension of field_dict.
        # When the input field exceeds 2 dimensions, convert TagFeature to 2D output.
        if len(field.get_shape()) < 2 or field.get_shape()[-1] == 1:
          if len(field.get_shape()) == 0:
            field = tf.expand_dims(field, axis=0)
          elif len(field.get_shape()) == 2:
            field = tf.squeeze(field, axis=-1)
          parsed_dict[input_0] = tf.string_split(field, fc.separator)
          if fc.HasField('kv_separator'):
            indices = parsed_dict[input_0].indices
            tmp_kvs = parsed_dict[input_0].values
            tmp_kvs = tf.string_split(
                tmp_kvs, fc.kv_separator, skip_empty=False)
            tmp_kvs = tf.reshape(tmp_kvs.values, [-1, 2])
            tmp_ks, tmp_vs = tmp_kvs[:, 0], tmp_kvs[:, 1]
            tmp_vs = tf.string_to_number(
                tmp_vs, tf.float32, name='kv_tag_wgt_str_2_flt_%s' % input_0)
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                indices, tmp_ks, parsed_dict[input_0].dense_shape)
            input_wgt = input_0 + '_WEIGHT'
            parsed_dict[input_wgt] = tf.sparse.SparseTensor(
                indices, tmp_vs, parsed_dict[input_0].dense_shape)
            self._appended_fields.append(input_wgt)
          if not fc.HasField('hash_bucket_size'):
            vals = tf.string_to_number(
                parsed_dict[input_0].values,
                tf.int32,
                name='tag_fea_%s' % input_0)
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                parsed_dict[input_0].indices, vals,
                parsed_dict[input_0].dense_shape)
          if len(fc.input_names) > 1:
            input_1 = fc.input_names[1]
            field = field_dict[input_1]
            if len(field.get_shape()) == 0:
              field = tf.expand_dims(field, axis=0)
            field = tf.string_split(field, fc.separator)
            field_vals = tf.string_to_number(
                field.values, tf.float32, name='tag_wgt_str_2_flt_%s' % input_1)
            assert_op = tf.assert_equal(
                tf.shape(field_vals)[0],
                tf.shape(parsed_dict[input_0].values)[0],
                message='tag_feature_kv_size_not_eq_%s' % input_0)
            with tf.control_dependencies([assert_op]):
              field = tf.sparse.SparseTensor(field.indices,
                                             tf.identity(field_vals),
                                             field.dense_shape)
            parsed_dict[input_1] = field
        else:
          parsed_dict[input_0] = field_dict[input_0]
          if len(fc.input_names) > 1:
            input_1 = fc.input_names[1]
            parsed_dict[input_1] = field_dict[input_1]
      elif feature_type == fc.LookupFeature:
        assert feature_name is not None and feature_name != ''
        assert len(fc.input_names) == 2
        parsed_dict[feature_name] = self._lookup_preprocess(fc, field_dict)
      elif feature_type == fc.SequenceFeature:
        input_0 = fc.input_names[0]
        field = field_dict[input_0]
        sub_feature_type = fc.sub_feature_type
        # Construct the output of SeqFeature according to the dimension of field_dict.
        # When the input field exceeds 2 dimensions, convert SeqFeature to 2D output.
        if len(field.get_shape()) < 2:
          parsed_dict[input_0] = tf.strings.split(field, fc.separator)
          if fc.HasField('seq_multi_sep'):
            indices = parsed_dict[input_0].indices
            values = parsed_dict[input_0].values
            multi_vals = tf.string_split(values, fc.seq_multi_sep)
            indices_1 = multi_vals.indices
            indices = tf.gather(indices, indices_1[:, 0])
            out_indices = tf.concat([indices, indices_1[:, 1:]], axis=1)
            # 3 dimensional sparse tensor
            out_shape = tf.concat(
                [parsed_dict[input_0].dense_shape, multi_vals.dense_shape[1:]],
                axis=0)
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                out_indices, multi_vals.values, out_shape)
          if (fc.num_buckets > 1 and fc.max_val == fc.min_val):
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                parsed_dict[input_0].indices,
                tf.string_to_number(
                    parsed_dict[input_0].values,
                    tf.int64,
                    name='sequence_str_2_int_%s' % input_0),
                parsed_dict[input_0].dense_shape)
          elif sub_feature_type == fc.RawFeature:
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                parsed_dict[input_0].indices,
                tf.string_to_number(
                    parsed_dict[input_0].values,
                    tf.float32,
                    name='sequence_str_2_float_%s' % input_0),
                parsed_dict[input_0].dense_shape)
          if fc.num_buckets > 1 and fc.max_val > fc.min_val:
            normalized_values = (parsed_dict[input_0].values - fc.min_val) / (
                fc.max_val - fc.min_val)
            parsed_dict[input_0] = tf.sparse.SparseTensor(
                parsed_dict[input_0].indices, normalized_values,
                parsed_dict[input_0].dense_shape)
        else:
          parsed_dict[input_0] = field
        if not fc.boundaries and fc.num_buckets <= 1 and fc.hash_bucket_size <= 0 and \
            self._data_config.sample_weight != input_0 and sub_feature_type == fc.RawFeature and \
            fc.raw_input_dim == 1:
          # may need by wide model and deep model to project
          # raw values to a vector, it maybe better implemented
          # by a ProjectionColumn later
          logging.info(
              'Not set boundaries or num_buckets or hash_bucket_size, %s will process as two dimentsion raw feature'
              % input_0)
          parsed_dict[input_0] = tf.sparse_to_dense(
              parsed_dict[input_0].indices,
              [tf.shape(parsed_dict[input_0])[0], fc.sequence_length],
              parsed_dict[input_0].values)
          sample_num = tf.to_int64(tf.shape(parsed_dict[input_0])[0])
          indices_0 = tf.range(sample_num, dtype=tf.int64)
          indices_1 = tf.range(fc.sequence_length, dtype=tf.int64)
          indices_0 = indices_0[:, None]
          indices_1 = indices_1[None, :]
          indices_0 = tf.tile(indices_0, [1, fc.sequence_length])
          indices_1 = tf.tile(indices_1, [sample_num, 1])
          indices_0 = tf.reshape(indices_0, [-1, 1])
          indices_1 = tf.reshape(indices_1, [-1, 1])
          indices = tf.concat([indices_0, indices_1], axis=1)
          parsed_dict[input_0 + '_raw_proj_id'] = tf.SparseTensor(
              indices=indices,
              values=indices_1[:, 0],
              dense_shape=[sample_num, fc.sequence_length])
          parsed_dict[input_0 + '_raw_proj_val'] = tf.SparseTensor(
              indices=indices,
              values=tf.reshape(parsed_dict[input_0], [-1]),
              dense_shape=[sample_num, fc.sequence_length])
          self._appended_fields.append(input_0 + '_raw_proj_id')
          self._appended_fields.append(input_0 + '_raw_proj_val')
        elif not fc.boundaries and fc.num_buckets <= 1 and fc.hash_bucket_size <= 0 and \
            self._data_config.sample_weight != input_0 and sub_feature_type == fc.RawFeature and \
            fc.raw_input_dim > 1:
          # for 3 dimension sequence feature input.
          # may need by wide model and deep model to project
          # raw values to a vector, it maybe better implemented
          # by a ProjectionColumn later
          logging.info(
              'Not set boundaries or num_buckets or hash_bucket_size, %s will process as three dimentsion raw feature'
              % input_0)
          parsed_dict[input_0] = tf.sparse_to_dense(
              parsed_dict[input_0].indices, [
                  tf.shape(parsed_dict[input_0])[0], fc.sequence_length,
                  fc.raw_input_dim
              ], parsed_dict[input_0].values)
          sample_num = tf.to_int64(tf.shape(parsed_dict[input_0])[0])
          indices_0 = tf.range(sample_num, dtype=tf.int64)
          indices_1 = tf.range(fc.sequence_length, dtype=tf.int64)
          indices_2 = tf.range(fc.raw_input_dim, dtype=tf.int64)
          indices_0 = indices_0[:, None, None]
          indices_1 = indices_1[None, :, None]
          indices_2 = indices_2[None, None, :]
          indices_0 = tf.tile(indices_0,
                              [1, fc.sequence_length, fc.raw_input_dim])
          indices_1 = tf.tile(indices_1, [sample_num, 1, fc.raw_input_dim])
          indices_2 = tf.tile(indices_2, [sample_num, fc.sequence_length, 1])
          indices_0 = tf.reshape(indices_0, [-1, 1])
          indices_1 = tf.reshape(indices_1, [-1, 1])
          indices_2 = tf.reshape(indices_2, [-1, 1])
          indices = tf.concat([indices_0, indices_1, indices_2], axis=1)

          parsed_dict[input_0 + '_raw_proj_id'] = tf.SparseTensor(
              indices=indices,
              values=indices_1[:, 0],
              dense_shape=[sample_num, fc.sequence_length, fc.raw_input_dim])
          parsed_dict[input_0 + '_raw_proj_val'] = tf.SparseTensor(
              indices=indices,
              values=tf.reshape(parsed_dict[input_0], [-1]),
              dense_shape=[sample_num, fc.sequence_length, fc.raw_input_dim])
          self._appended_fields.append(input_0 + '_raw_proj_id')
          self._appended_fields.append(input_0 + '_raw_proj_val')
      elif feature_type == fc.RawFeature:
        input_0 = fc.input_names[0]
        if field_dict[input_0].dtype == tf.string:
          if fc.raw_input_dim > 1:
            tmp_fea = tf.string_split(field_dict[input_0], fc.separator)
            tmp_vals = tf.string_to_number(
                tmp_fea.values,
                tf.float32,
                name='multi_raw_fea_to_flt_%s' % input_0)
            parsed_dict[input_0] = tf.sparse_to_dense(
                tmp_fea.indices,
                [tf.shape(field_dict[input_0])[0], fc.raw_input_dim],
                tmp_vals,
                default_value=0)
          else:
            parsed_dict[input_0] = tf.string_to_number(field_dict[input_0],
                                                       tf.float32)
        elif field_dict[input_0].dtype in [
            tf.int32, tf.int64, tf.double, tf.float32
        ]:
          parsed_dict[input_0] = tf.to_float(field_dict[input_0])
        else:
          assert False, 'invalid dtype[%s] for raw feature' % str(
              field_dict[input_0].dtype)
        if fc.max_val > fc.min_val:
          parsed_dict[input_0] = (parsed_dict[input_0] - fc.min_val) /\
                                 (fc.max_val - fc.min_val)
        if not fc.boundaries and fc.num_buckets <= 1 and \
            self._data_config.sample_weight != input_0:
          # may need by wide model and deep model to project
          # raw values to a vector, it maybe better implemented
          # by a ProjectionColumn later
          sample_num = tf.to_int64(tf.shape(parsed_dict[input_0])[0])
          indices_0 = tf.range(sample_num, dtype=tf.int64)
          indices_1 = tf.range(fc.raw_input_dim, dtype=tf.int64)
          indices_0 = indices_0[:, None]
          indices_1 = indices_1[None, :]
          indices_0 = tf.tile(indices_0, [1, fc.raw_input_dim])
          indices_1 = tf.tile(indices_1, [sample_num, 1])
          indices_0 = tf.reshape(indices_0, [-1, 1])
          indices_1 = tf.reshape(indices_1, [-1, 1])
          indices = tf.concat([indices_0, indices_1], axis=1)

          parsed_dict[input_0 + '_raw_proj_id'] = tf.SparseTensor(
              indices=indices,
              values=indices_1[:, 0],
              dense_shape=[sample_num, fc.raw_input_dim])
          parsed_dict[input_0 + '_raw_proj_val'] = tf.SparseTensor(
              indices=indices,
              values=tf.reshape(parsed_dict[input_0], [-1]),
              dense_shape=[sample_num, fc.raw_input_dim])
          self._appended_fields.append(input_0 + '_raw_proj_id')
          self._appended_fields.append(input_0 + '_raw_proj_val')
      elif feature_type == fc.IdFeature:
        input_0 = fc.input_names[0]
        parsed_dict[input_0] = field_dict[input_0]
        if fc.HasField('hash_bucket_size'):
          if field_dict[input_0].dtype != tf.string:
            if field_dict[input_0].dtype in [tf.float32, tf.double]:
              assert fc.precision > 0, 'it is dangerous to convert float or double to string due to ' \
                                       'precision problem, it is suggested to convert them into string ' \
                                       'format during feature generalization before using EasyRec; ' \
                                       'if you really need to do so, please set precision (the number of ' \
                                       'decimal digits) carefully.'
            precision = None
            if field_dict[input_0].dtype in [tf.float32, tf.double]:
              if fc.precision > 0:
                precision = fc.precision
            # convert to string
            if 'as_string' in dir(tf.strings):
              parsed_dict[input_0] = tf.strings.as_string(
                  field_dict[input_0], precision=precision)
            else:
              parsed_dict[input_0] = tf.as_string(
                  field_dict[input_0], precision=precision)
        elif fc.num_buckets > 0:
          if parsed_dict[input_0].dtype == tf.string:
            parsed_dict[input_0] = tf.string_to_number(
                parsed_dict[input_0], tf.int32, name='%s_str_2_int' % input_0)

      elif feature_type == fc.ExprFeature:
        fea_name = fc.feature_name
        prefix = "expr_"
        for input_name in fc.input_names:
            new_input_name = prefix + input_name
            if field_dict[input_name].dtype == tf.string:
                parsed_dict[new_input_name] = tf.string_to_number(
                    field_dict[input_name], tf.float64, name='%s_str_2_int_for_expr' % new_input_name)
            elif field_dict[input_name].dtype in [tf.int32, tf.int64, tf.double, tf.float32]:
                parsed_dict[new_input_name] = tf.cast(field_dict[input_name], tf.float64)
            else:
                assert False, 'invalid input dtype[%s] for expr feature' % str(field_dict[input_name].dtype)

        expression = get_expression(fc.expression, fc.input_names, prefix=prefix)
        logging.info("expression: %s" % expression)
        parsed_dict[fea_name] = eval(expression)
        self._appended_fields.append(fea_name)

      else:
        for input_name in fc.input_names:
          parsed_dict[input_name] = field_dict[input_name]

    for input_id, input_name in enumerate(self._label_fields):
      if input_name not in field_dict:
        continue
      if field_dict[input_name].dtype == tf.string:
        if self._label_dim[input_id] > 1:
          logging.info('will split labels[%d]=%s' % (input_id, input_name))
          parsed_dict[input_name] = tf.string_split(
              field_dict[input_name], self._label_sep[input_id]).values
          parsed_dict[input_name] = tf.reshape(parsed_dict[input_name],
                                               [-1, self._label_dim[input_id]])
        else:
          parsed_dict[input_name] = field_dict[input_name]
        parsed_dict[input_name] = tf.string_to_number(
            parsed_dict[input_name], tf.float32, name=input_name)
      else:
        assert field_dict[input_name].dtype in [
            tf.float32, tf.double, tf.int32, tf.int64
        ], 'invalid label dtype: %s' % str(field_dict[input_name].dtype)
        parsed_dict[input_name] = field_dict[input_name]

    if self._data_config.HasField('sample_weight'):
      if self._mode != tf.estimator.ModeKeys.PREDICT:
        parsed_dict[constant.SAMPLE_WEIGHT] = field_dict[
            self._data_config.sample_weight]
    return parsed_dict

  def _lookup_preprocess(self, fc, field_dict):
    """Preprocess function for lookup features.

    Args:
      fc: FeatureConfig
      field_dict: input dict

    Returns:
      output_dict: add { feature_name:SparseTensor} with
          other items similar as field_dict
    """
    max_sel_num = fc.lookup_max_sel_elem_num

    def _lookup(args, pad=True):
      one_key, one_map = args[0], args[1]
      if len(one_map.get_shape()) == 0:
        one_map = tf.expand_dims(one_map, axis=0)
      kv_map = tf.string_split(one_map, fc.separator).values
      kvs = tf.string_split(kv_map, fc.kv_separator)
      kvs = tf.reshape(kvs.values, [-1, 2], name='kv_split_reshape')
      keys, vals = kvs[:, 0], kvs[:, 1]
      sel_ids = tf.where(tf.equal(keys, one_key))
      sel_ids = tf.squeeze(sel_ids, axis=1)
      sel_vals = tf.gather(vals, sel_ids)
      if not pad:
        return sel_vals
      n = tf.shape(sel_vals)[0]
      sel_vals = tf.pad(sel_vals, [[0, max_sel_num - n]])
      len_msk = tf.sequence_mask(n, max_sel_num)
      indices = tf.range(max_sel_num, dtype=tf.int64)
      indices = indices * tf.to_int64(indices < tf.to_int64(n))
      return sel_vals, len_msk, indices

    key_field, map_field = fc.input_names[0], fc.input_names[1]
    key_fields, map_fields = field_dict[key_field], field_dict[map_field]
    if len(key_fields.get_shape()) == 0:
      vals = _lookup((key_fields, map_fields), False)
      n = tf.shape(vals)[0]
      n = tf.to_int64(n)
      indices_0 = tf.zeros([n], dtype=tf.int64)
      indices_1 = tf.range(0, n, dtype=tf.int64)
      indices = [
          tf.expand_dims(indices_0, axis=1),
          tf.expand_dims(indices_1, axis=1)
      ]
      indices = tf.concat(indices, axis=1)
      return tf.sparse.SparseTensor(indices, vals, [1, n])

    vals, masks, indices = tf.map_fn(
        _lookup, [key_fields, map_fields], dtype=(tf.string, tf.bool, tf.int64))
    batch_size = tf.to_int64(tf.shape(vals)[0])
    vals = tf.boolean_mask(vals, masks)
    indices_1 = tf.boolean_mask(indices, masks)
    indices_0 = tf.range(0, batch_size, dtype=tf.int64)
    indices_0 = tf.expand_dims(indices_0, axis=1)
    indices_0 = indices_0 + tf.zeros([1, max_sel_num], dtype=tf.int64)
    indices_0 = tf.boolean_mask(indices_0, masks)
    indices = tf.concat(
        [tf.expand_dims(indices_0, axis=1),
         tf.expand_dims(indices_1, axis=1)],
        axis=1)
    shapes = tf.stack([batch_size, tf.reduce_max(indices_1) + 1])
    return tf.sparse.SparseTensor(indices, vals, shapes)

  @abstractmethod
  def _build(self, mode, params):
    raise NotImplementedError

  def _pre_build(self, mode, params):
    pass

  def create_input(self, export_config=None):

    def _input_fn(mode=None, params=None, config=None):
      """Build input_fn for estimator.

      Args:
        mode: tf.estimator.ModeKeys.(TRAIN, EVAL, PREDICT)
            params: `dict` of hyper parameters, from Estimator
            config: tf.estimator.RunConfig instance

      Return:
        if mode is not None, return:
            features: inputs to the model.
            labels: groundtruth
        else, return:
            tf.estimator.export.ServingInputReceiver instance
      """
      self._pre_build(mode, params)
      if mode in (tf.estimator.ModeKeys.TRAIN, tf.estimator.ModeKeys.EVAL,
                  tf.estimator.ModeKeys.PREDICT):
        # build dataset from self._config.input_path
        self._mode = mode
        dataset = self._build(mode, params)
        return dataset
      elif mode is None:  # serving_input_receiver_fn for export SavedModel
        if export_config.multi_placeholder:
          inputs, features = self.create_multi_placeholders(export_config)
          return tf.estimator.export.ServingInputReceiver(features, inputs)
        else:
          inputs, features = self.create_placeholders(export_config)
          print("built feature placeholders. features: {}".format(features.keys()))
          return tf.estimator.export.ServingInputReceiver(features, inputs)

    return _input_fn
