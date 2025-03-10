# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
"""Contains functions which are convenient for unit testing.

isort:skip_file
"""
from future import standard_library
standard_library.install_aliases()
import yaml
import glob
import json
import logging
import os
import random
import shutil
import string
import subprocess
import time
from multiprocessing import Process
from subprocess import getstatusoutput
from tensorflow.python.platform import gfile
import numpy as np
from easy_rec.python.protos.train_pb2 import DistributionStrategy
from easy_rec.python.utils import config_util
from easy_rec.python.protos.pipeline_pb2 import EasyRecConfig

TEST_DIR = './tmp/easy_rec_test'


def get_hdfs_tmp_dir(test_dir):
  """Create a randomly of directory  in HDFS."""
  tmp_name = ''.join(
      [random.choice(string.ascii_letters + string.digits) for i in range(8)])
  assert isinstance(test_dir, str)
  test_rand_dir = os.path.join(test_dir, tmp_name)
  gfile.MkDir(test_rand_dir)
  return test_rand_dir


def get_tmp_dir():
  tmp_name = ''.join(
      [random.choice(string.ascii_letters + string.digits) for i in range(8)])
  if os.environ.get('TEST_DIR', '') != '':
    global TEST_DIR
    TEST_DIR = os.environ['TEST_DIR']
  dir_name = os.path.join(TEST_DIR, tmp_name)
  if os.path.exists(dir_name):
    shutil.rmtree(dir_name)
  os.makedirs(dir_name)
  return dir_name


def clear_all_tmp_dirs():
  shutil.rmtree(TEST_DIR)


def set_gpu_id(gpu_id_str):
  env = os.environ
  if gpu_id_str is None:
    env['CUDA_VISIBLE_DEVICES'] = ''
  else:
    env['CUDA_VISIBLE_DEVICES'] = gpu_id_str


def get_available_gpus():
  if 'TEST_DEVICES' in os.environ:
    gpus = os.environ['TEST_DEVICES'].split(',')
  else:
    gpus = glob.glob('/dev/nvidia[0-9]*')
    gpus = [gpu.replace('/dev/nvidia', '') for gpu in gpus]
  logging.info('available gpus %s' % gpus)
  return gpus


def run_cmd(cmd_str, log_file):
  """Run a shell cmd."""
  cmd_str = cmd_str.replace('\r', ' ').replace('\n', ' ')
  logging.info('RUNCMD: %s > %s 2>&1 ' % (cmd_str, log_file))
  with open(log_file, 'w') as lfile:
    return subprocess.Popen(
        cmd_str, stdout=lfile, stderr=subprocess.STDOUT, shell=True)


def RunAsSubprocess(f):
  """Function dectorator to run function in subprocess.

  if a function will start a tf session. Because tensorflow gpu memory will not be cleared until the
  process exit.
  """

  def wrapped_f(*args, **kw):
    p = Process(target=f, args=args, kwargs=kw)
    p.start()
    p.join(timeout=600)
    assert p.exitcode == 0, 'subprocess run failed: %s' % f.__name__

  return wrapped_f


def clean_up(test_dir):
  if test_dir is not None:
    shutil.rmtree(test_dir)

  # reset to cpu mode
  set_gpu_id(None)


def clean_up_hdfs(test_dir):
  if gfile.Exists(test_dir):
    gfile.DeleteRecursively(test_dir)
  set_gpu_id(None)


def _replace_data_for_test(data_path):
  """Replace real data with test data."""
  test_data = {}

  change = False
  releated_datasets = []
  for k, config in test_data.items():
    if k in data_path:
      releated_datasets.append(k)

  # if there are multiple keyword detected, use the longest one
  if len(releated_datasets) > 0:
    score = [len(k) for k in releated_datasets]
    best_match = np.argmax(score)
    data_path = test_data[releated_datasets[best_match]]

    change = True
  assert change, 'Failed to replace data with test data'

  return data_path


def _load_config_for_test(pipeline_config_path, test_dir, total_steps=50):
  pipeline_config = config_util.get_configs_from_pipeline_file(
      pipeline_config_path)
  train_config = pipeline_config.train_config
  eval_config = pipeline_config.eval_config
  data_config = pipeline_config.data_config

  train_config.num_steps = total_steps
  # change model_dir
  pipeline_config.model_dir = test_dir + '/train'
  logging.info('test_model_dir %s' % pipeline_config.model_dir)
  eval_config.num_examples = max(10, data_config.batch_size)
  data_config.num_epochs = 0
  return pipeline_config


def test_datahub_train_eval(pipeline_config_path,
                            test_dir,
                            process_pipeline_func=None,
                            hyperparam_str='',
                            total_steps=50,
                            post_check_func=None):
  gpus = get_available_gpus()
  if len(gpus) > 0:
    set_gpu_id(gpus[0])
  else:
    set_gpu_id(None)

  if not isinstance(pipeline_config_path, EasyRecConfig):
    logging.info('testing pipeline config %s' % pipeline_config_path)
  if 'TF_CONFIG' in os.environ:
    del os.environ['TF_CONFIG']

  if isinstance(pipeline_config_path, EasyRecConfig):
    pipeline_config = pipeline_config_path
  else:
    pipeline_config = _load_config_for_test(pipeline_config_path, test_dir,
                                            total_steps)

  pipeline_config.train_config.train_distribute = 0
  pipeline_config.train_config.num_gpus_per_worker = 1
  pipeline_config.train_config.sync_replicas = False
  if process_pipeline_func is not None:
    assert callable(process_pipeline_func)
    pipeline_config = process_pipeline_func(pipeline_config)
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')
  train_cmd = 'python3 -m easy_rec.python.train_eval --pipeline_config_path %s %s' % (
      test_pipeline_config_path, hyperparam_str)
  proc = run_cmd(train_cmd, '%s/log_%s.txt' % (test_dir, 'master'))
  proc.wait()
  if proc.returncode != 0:
    logging.error('train %s failed' % test_pipeline_config_path)
    return False
  if post_check_func:
    return post_check_func(pipeline_config)
  return True


def _Load_config_for_test_eval(pipeline_config_path):
  pipeline_config = config_util.get_configs_from_pipeline_file(
      pipeline_config_path)
  return pipeline_config


def test_single_train_eval(pipeline_config_path,
                           test_dir,
                           process_pipeline_func=None,
                           hyperparam_str='',
                           total_steps=50,
                           post_check_func=None):
  gpus = get_available_gpus()
  if len(gpus) > 0:
    set_gpu_id(gpus[0])
  else:
    set_gpu_id(None)

  if not isinstance(pipeline_config_path, EasyRecConfig):
    logging.info('testing pipeline config %s' % pipeline_config_path)
  if 'TF_CONFIG' in os.environ:
    del os.environ['TF_CONFIG']

  if isinstance(pipeline_config_path, EasyRecConfig):
    pipeline_config = pipeline_config_path
  else:
    pipeline_config = _load_config_for_test(pipeline_config_path, test_dir,
                                            total_steps)

  pipeline_config.train_config.train_distribute = 0
  pipeline_config.train_config.num_gpus_per_worker = 1
  pipeline_config.train_config.sync_replicas = False
  if process_pipeline_func is not None:
    assert callable(process_pipeline_func)
    pipeline_config = process_pipeline_func(pipeline_config)
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')
  train_cmd = 'python -m easy_rec.python.train_eval --pipeline_config_path %s %s' % (
      test_pipeline_config_path, hyperparam_str)
  proc = run_cmd(train_cmd, '%s/log_%s.txt' % (test_dir, 'master'))
  proc.wait()
  if proc.returncode != 0:
    logging.error('train %s failed' % test_pipeline_config_path)
    return False
  if post_check_func:
    return post_check_func(pipeline_config)
  return True


def test_feature_selection(pipeline_config):
  model_dir = pipeline_config.model_dir
  pipeline_config_path = os.path.join(model_dir, 'pipeline.config')
  output_dir = os.path.join(model_dir, 'feature_selection')
  cmd = 'python -m easy_rec.python.tools.feature_selection --config_path %s ' \
        '--output_dir %s --topk 5 --visualize true' % (pipeline_config_path, output_dir)
  proc = run_cmd(cmd, os.path.join(model_dir, 'log_feature_selection.txt'))
  proc.wait()
  if proc.returncode != 0:
    logging.error('feature selection %s failed' % pipeline_config_path)
    return False
  return True


def yaml_replace(train_yaml_path,
                 pipline_config_path,
                 test_pipeline_config_path,
                 test_export_dir=None):
  with open(train_yaml_path, 'r', encoding='utf-8') as _file:
    sample = _file.read()
    x = yaml.load(sample)
    _command = x['app']['command']
    if test_export_dir is not None:
      _command = _command.replace(pipline_config_path,
                                  test_pipeline_config_path).replace(
                                      '{EXPOERT_DIR}', test_export_dir)
    else:
      _command = _command.replace(pipline_config_path,
                                  test_pipeline_config_path)
    x['app']['command'] = _command

  with open(train_yaml_path, 'w', encoding='utf-8') as _file:
    yaml.dump(x, _file)


def test_hdfs_train_eval(pipeline_config_path,
                         train_yaml_path,
                         test_dir,
                         process_pipeline_func=None,
                         hyperparam_str='',
                         total_steps=2000):

  gpus = get_available_gpus()
  if len(gpus) > 0:
    set_gpu_id(gpus[0])
  else:
    set_gpu_id(None)
  logging.info('testing pipeline config %s' % pipeline_config_path)
  logging.info('train_yaml_path %s' % train_yaml_path)
  if 'TF_CONFIG' in os.environ:
    del os.environ['TF_CONFIG']
  pipeline_config = _load_config_for_test(pipeline_config_path, test_dir,
                                          total_steps)
  logging.info('model_dir in pipeline_config has been modified')
  pipeline_config.train_config.train_distribute = 0
  pipeline_config.train_config.num_gpus_per_worker = 1
  pipeline_config.train_config.sync_replicas = False
  if process_pipeline_func is not None:
    assert callable(process_pipeline_func)
    pipeline_config = process_pipeline_func(pipeline_config)
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')
  yaml_replace(train_yaml_path, pipeline_config_path, test_pipeline_config_path)
  logging.info('test_pipeline_config_path is %s' % test_pipeline_config_path)
  train_cmd = 'el_submit -yaml %s' % train_yaml_path
  proc = subprocess.Popen(train_cmd.split(), stderr=subprocess.STDOUT)
  proc.wait()
  if proc.returncode != 0:
    logging.error('train %s failed' % test_pipeline_config_path)
    logging.error('train_yaml %s failed' % train_yaml_path)
  return proc.returncode == 0


def test_hdfs_eval(pipeline_config_path,
                   eval_yaml_path,
                   test_dir,
                   process_pipeline_func=None,
                   hyperparam_str=''):

  gpus = get_available_gpus()
  if len(gpus) > 0:
    set_gpu_id(gpus[0])
  else:
    set_gpu_id(None)
  logging.info('testing export pipeline config %s' % pipeline_config_path)
  logging.info('eval_yaml_path %s' % eval_yaml_path)
  if 'TF_CONFIG' in os.environ:
    del os.environ['TF_CONFIG']
  pipeline_config = _Load_config_for_test_eval(pipeline_config_path)
  if process_pipeline_func is not None:
    assert callable(process_pipeline_func)
    pipeline_config = process_pipeline_func(pipeline_config)
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')
  yaml_replace(eval_yaml_path, pipeline_config_path, test_pipeline_config_path)
  logging.info('test_pipeline_config_path is %s' % test_pipeline_config_path)
  eval_cmd = 'el_submit -yaml %s' % eval_yaml_path
  proc = subprocess.Popen(eval_cmd.split(), stderr=subprocess.STDOUT)
  proc.wait()
  if proc.returncode != 0:
    logging.error('eval %s failed' % test_pipeline_config_path)
    logging.error('eval_yaml %s failed' % eval_yaml_path)
  return proc.returncode == 0


def test_hdfs_export(pipeline_config_path,
                     export_yaml_path,
                     test_dir,
                     process_pipeline_func=None,
                     hyperparam_str=''):

  gpus = get_available_gpus()
  if len(gpus) > 0:
    set_gpu_id(gpus[0])
  else:
    set_gpu_id(None)
  logging.info('testing export pipeline config %s' % pipeline_config_path)
  logging.info('export_yaml_path %s' % export_yaml_path)
  if 'TF_CONFIG' in os.environ:
    del os.environ['TF_CONFIG']
  pipeline_config = _Load_config_for_test_eval(pipeline_config_path)
  if process_pipeline_func is not None:
    assert callable(process_pipeline_func)
    pipeline_config = process_pipeline_func(pipeline_config)
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')
  test_export_path = os.path.join(test_dir, 'export_dir')
  yaml_replace(export_yaml_path, pipeline_config_path,
               test_pipeline_config_path, test_export_path)
  logging.info('test_pipeline_config_path is %s' % test_pipeline_config_path)
  eval_cmd = 'el_submit -yaml %s' % export_yaml_path
  proc = subprocess.Popen(eval_cmd.split(), stderr=subprocess.STDOUT)
  proc.wait()
  if proc.returncode != 0:
    logging.error('export %s failed' % test_pipeline_config_path)
    logging.error('export_yaml %s failed' % export_yaml_path)
  return proc.returncode == 0


def _ports_in_use(ports):
  ports_str = ''
  for i, port in enumerate(ports):
    if i > 0:
      ports_str += '|'
    ports_str += '0.0.0.0:%d|127.0.0.1:%d' % (port, port)
  stat, output = getstatusoutput('netstat -tlnp | grep -E %s' % ports_str)
  return stat == 0


def _get_ports(num_worker):
  port_base = int(os.environ.get('PORT_BASE', 10000))
  num_try = 10
  for i in range(num_try):
    ports = np.random.randint(port_base, port_base + 5000, size=num_worker)
    if not _ports_in_use(ports):
      return ports
    logging.info('ports %s in use, retry...' % ports)


def _ps_worker_train(pipeline_config_path,
                     test_dir,
                     num_worker,
                     num_evaluator=0):
  gpus = get_available_gpus()
  # not enough gpus, run on cpu only
  if len(gpus) < num_worker:
    gpus = [None] * num_worker
  ports = _get_ports(num_worker + 1)
  chief_or_master = 'master' if num_evaluator == 0 else 'chief'
  cluster = {
      chief_or_master: ['localhost:%d' % ports[0]],
      'worker': ['localhost:%d' % ports[i] for i in range(1, num_worker)],
      'ps': ['localhost:%d' % ports[-1]]
  }
  tf_config = {'cluster': cluster}
  procs = {}
  tf_config['task'] = {'type': chief_or_master, 'index': 0}
  os.environ['TF_CONFIG'] = json.dumps(tf_config)
  set_gpu_id(gpus[0])
  train_cmd = 'python -m easy_rec.python.train_eval --pipeline_config_path %s' % pipeline_config_path
  procs[chief_or_master] = run_cmd(
      train_cmd, '%s/log_%s.txt' % (test_dir, chief_or_master))
  tf_config['task'] = {'type': 'ps', 'index': 0}
  os.environ['TF_CONFIG'] = json.dumps(tf_config)
  set_gpu_id('')
  procs['ps'] = run_cmd(train_cmd, '%s/log_%s.txt' % (test_dir, 'ps'))

  for idx in range(num_worker - 1):
    tf_config['task'] = {'type': 'worker', 'index': idx}
    os.environ['TF_CONFIG'] = json.dumps(tf_config)
    set_gpu_id(gpus[idx + 1])
    worker_name = 'worker_%d' % idx
    procs[worker_name] = run_cmd(train_cmd,
                                 '%s/log_%s.txt' % (test_dir, worker_name))
  if num_evaluator > 0:
    tf_config['task'] = {'type': 'evaluator', 'index': 0}
    os.environ['TF_CONFIG'] = json.dumps(tf_config)
    set_gpu_id('')
    procs['evaluator'] = run_cmd(train_cmd,
                                 '%s/log_%s.txt' % (test_dir, 'evaluator'))

  return procs


def _multi_worker_mirror_train(pipeline_config_path, test_dir, num_worker):
  gpus = get_available_gpus()
  # not enough gpus, run on cpu only
  if len(gpus) < num_worker:
    gpus = [None] * num_worker
  ports = _get_ports(num_worker)
  tf_config = {
      'cluster': {
          'worker': ['localhost:%d' % ports[i] for i in range(num_worker)]
      }
  }
  procs = {}
  train_cmd = 'python -m easy_rec.python.train_eval --pipeline_config_path %s' % pipeline_config_path
  for idx in range(num_worker):
    tf_config['task'] = {'type': 'worker', 'index': idx}
    os.environ['TF_CONFIG'] = json.dumps(tf_config)
    set_gpu_id(gpus[idx])
    worker_name = 'worker_%d' % idx
    procs[worker_name] = run_cmd(train_cmd,
                                 '%s/log_%s.txt' % (test_dir, worker_name))
  return procs


def test_distributed_train_eval(pipeline_config_path,
                                test_dir,
                                total_steps=50,
                                num_evaluator=0):
  logging.info('testing pipeline config %s' % pipeline_config_path)
  pipeline_config = _load_config_for_test(pipeline_config_path, test_dir,
                                          total_steps)
  train_config = pipeline_config.train_config
  config_util.save_pipeline_config(pipeline_config, test_dir)
  test_pipeline_config_path = os.path.join(test_dir, 'pipeline.config')

  task_failed = None
  procs = None
  try:
    if train_config.train_distribute == DistributionStrategy.NoStrategy:
      num_worker = 2
      procs = _ps_worker_train(test_pipeline_config_path, test_dir, num_worker,
                               num_evaluator)
    elif train_config.train_distribute == DistributionStrategy.MultiWorkerMirroredStrategy:
      num_worker = 2
      procs = _multi_worker_mirror_train(test_pipeline_config_path, test_dir,
                                         num_worker)
    else:
      raise NotImplementedError

    # print proc info
    assert len(procs) > 0, 'processes are empty'
    for k, proc in procs.items():
      logging.info('%s pid: %d' % (k, proc.pid))
    task_finish_cnt = 0
    task_has_finished = {k: False for k in procs.keys()}
    while True:
      for k, proc in procs.items():
        if proc.poll() is None:
          if task_failed is not None:
            logging.error('task %s failed, %s quit' % (task_failed, k))
            proc.terminate()
            if k != 'ps':
              task_has_finished[k] = True
              task_finish_cnt += 1
            logging.info('task_finish_cnt %d' % task_finish_cnt)
        else:
          if not task_has_finished[k]:
            # process quit by itself
            if k != 'ps':
              task_finish_cnt += 1
              task_has_finished[k] = True
            logging.info('task_finish_cnt %d' % task_finish_cnt)
            if proc.returncode != 0:
              logging.error('%s failed' % k)
              task_failed = k
            else:
              logging.info('%s run successfuly' % k)

      if task_finish_cnt >= num_worker:
        break
      time.sleep(1)

  except Exception as e:
    logging.error('Exception: ' + str(e))
    raise e
  finally:
    if procs is not None:
      for k, proc in procs.items():
        if proc.poll() is None:
          logging.info('terminate %s' % k)
          proc.terminate()
    if task_failed is not None:
      logging.error('train %s failed' % pipeline_config_path)

  return task_failed is None
