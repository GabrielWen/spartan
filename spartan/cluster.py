'''
Functions for managing a cluster of machines.

Spartan currently supports running workers as either threads in the
current process, or by using ssh to connect to one or more 
machines.

A Spartan "worker" is a single process; more than one worker can be
run on a machine; typically one worker is run per core.
'''

import os.path
import socket
import subprocess
import sys
import threading
import time
import shutil
from spartan import rpc
from spartan import config, util
import spartan
from spartan.config import FLAGS, AssignMode
import spartan.master

class HostListFlag(config.Flag):
  def parse(self, str):
    hosts = []
    for host in str.split(','):
      hostname, count = host.split(':')
      hosts.append((hostname, int(count)))
    self.val = hosts

  def _str(self):
    return ','.join(['%s:%d' % (host, count) for host, count in self.val])

class AssignMode(object):
  BY_CORE = 1
  BY_NODE = 2

class AssignModeFlag(config.Flag):
  def parse(self, option_str):
    self.val = getattr(AssignMode, option_str)

  def _str(self):
    if self.val == AssignMode.BY_CORE: return 'BY_CORE'
    return 'BY_NODE'

FLAGS.add(HostListFlag('hosts', default=[('localhost', 8)]))
FLAGS.add(BoolFlag('xterm', default=False, help='Run workers in xterm'))
FLAGS.add(BoolFlag('oprofile', default=False, help='Run workers inside of operf'))
FLAGS.add(AssignModeFlag('assign_mode', default=AssignMode.BY_NODE))
FLAGS.add(BoolFlag('use_single_core', default=True))

FLAGS.add(BoolFlag(
  'use_threads',
  help='When running locally, use threads instead of forking. (slow, for debugging)',
  default=True))
FLAGS.add(IntFlag('heartbeat_interval', default=3, help='Heartbeat Interval in each worker'))
FLAGS.add(IntFlag('worker_failed_heartbeat_threshold', default=10, help='the max number of heartbeat that a worker can delay'))

def start_remote_worker(worker, st, ed):
  '''
  Start processes on a worker machine.
  
  The machine will launch worker processes ``st`` through ``ed``. 
  
  :param worker: hostname to connect to.
  :param st: First process index to start.
  :param ed: Last process to start.
  '''
  util.log_info('Starting worker %d:%d on host %s', st, ed, worker)
  if FLAGS.oprofile:
    os.system('mkdir operf.%s' % worker)
    
  ssh_args = ['ssh', '-oForwardX11=no', worker ]
 
  args = ['cd %s && ' % os.path.abspath(os.path.curdir)]

  if FLAGS.xterm:
    args += ['xterm', '-e',]

  if FLAGS.oprofile:
    args += ['operf -e CPU_CLK_UNHALTED:100000000', '-g', '-d', 'operf.%s' % worker]

  args += [
          #'gdb', '-ex', 'run', '--args',
          'spartan/worker',
          '--master=%s:%d' % (socket.gethostname(), FLAGS.port_base),
          '--count=%d' % (ed - st),
          '--heartbeat_interval=%d' % FLAGS.heartbeat_interval
          ]

  # add flags from config/user
  for (name, value) in FLAGS:
    if name in ['worker_list', 'print_options']: continue
    args += [repr(value)]
 
  #print >>sys.stderr, args 
  util.log_debug('Running worker %s', ' '.join(args))
  time.sleep(0.1)
  if worker != 'localhost':
    p = subprocess.Popen(ssh_args + args, executable='ssh')
  else:
    p = subprocess.Popen(' '.join(args), shell=True, stdin=subprocess.PIPE)
    
  return p

def start_cluster(num_workers, use_cluster_workers):
  '''
  Start a cluster with ``num_workers`` workers.
  
  If use_cluster_workers is True, then use the remote workers
  defined in `spartan.config`.  Otherwise, workers are all
  spawned on the localhost.
  
  :param num_workers:
  :param use_cluster_workers:
  '''
  rpc.set_default_timeout(FLAGS.default_rpc_timeout)
  #clean the checkpoint directory
  if os.path.exists(FLAGS.checkpoint_path):
    shutil.rmtree(FLAGS.checkpoint_path)
  
  master = spartan.master.Master(FLAGS.port_base, num_workers)

  if not use_cluster_workers:
    start_remote_worker('localhost', 0, num_workers)
  else:
    available_workers = sum([cnt for _, cnt in FLAGS.hosts])
    assert available_workers >= num_workers, 'Insufficient slots to run all workers.'
    count = 0
    num_hosts = len(FLAGS.hosts)
    for worker, total_tasks in FLAGS.hosts:
      if FLAGS.assign_mode == AssignMode.BY_CORE:
        sz = total_tasks
      else:
        sz = util.divup(num_workers, num_hosts)
      
      sz = min(sz, num_workers - count)
      start_remote_worker(worker, count, count + sz)
      count += sz
      if count == num_workers:
        break

  master.wait_for_initialization()
  return master

