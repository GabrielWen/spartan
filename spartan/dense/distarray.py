#!/usr/bin/env python

from . import tile, extent
import spartan
from spartan import util
from spartan.util import Assert
import numpy as np
import itertools
import collections

# number of elements per tile
TILE_SIZE = 100000

def find_matching_tile(array, tile_extent):
  for ex in array.extents():
    ul_diff = tile_extent.ul - ex.ul
    lr_diff = ex.lr - tile_extent.lr
    if np.all(ul_diff >= 0) and np.all(lr_diff >= 0):
      # util.log_info('%s matches %s', ex, tile_extent)
      return array.tile_for_extent(ex)
  
  raise Exception, 'No matching tile_extent!' 
 
 
def take_first(a,b):
  return a

accum_replace = tile.TileAccum(take_first)
accum_min = tile.TileAccum(np.minimum)
accum_max = tile.TileAccum(np.maximum)
accum_sum = tile.TileAccum(np.add)


  
class NestedSlice(object):
  def __init__(self, ex, subslice):
    Assert.isinstance(ex, extent.TileExtent)
    Assert.isinstance(subslice, (tuple, int))
    
    self.extent = ex
    self.subslice = subslice
    #util.log_info('Nested: %s[%s]', ex, subslice)
   
  def __eq__(self, other):
    Assert.isinstance(other, extent.TileExtent)
    return self.extent == other 
  
  def __hash__(self):
    return hash(self.extent)
  
  def shard(self):
    return self.extent.shard()


class TileSelector(object):
  def __call__(self, k, v):
    if isinstance(k, extent.TileExtent): 
      return v.get()
    
    if isinstance(k, NestedSlice):
      result = v[k.subslice]
#       print k.extent, k.subslice, result.shape
      return result
    raise Exception, "Can't handle type %s" % type(k)
  


def compute_splits(shape, tile_hint=None, num_shards=-1):
  '''Split an array of shape ``shape`` into `Extent`s containing roughly `TILE_SIZE` elements.
 
  :param shape: tuple
  :param tile_hint: tuple indicating the desired tile shape 
  :rtype: list of `Extent`
  '''
  
  splits = [None] * len(shape)
  if tile_hint is None:
    # try to make reasonable tiles
    if len(shape) == 0:
      return { extent.create([], [], ()) :  0 }
   
    weight = 1
    
    # split each dimension into tiles.  the first dimension
    # is kept contiguous if possible.
    for dim in reversed(range(len(shape))):
      step = max(1, TILE_SIZE / weight)
      dim_splits = []
      for i in range(0, shape[dim], step):
        dim_splits.append((i, min(shape[dim], i + step)))
        
      splits[dim] = dim_splits
      weight *= shape[dim]
  else:
    Assert.eq(len(tile_hint), len(shape),
              '#dimensions in tile hint does not match shape %s vs %s' % (tile_hint, shape))
    for dim in range(len(shape)):
      dim_splits = []
      step = tile_hint[dim]
      #Assert.le(step, shape[dim])
      for i in range(0, shape[dim], step):
        dim_splits.append((i, min(shape[dim],  i + step)))
      splits[dim] = dim_splits
 
  result = {}
  idx = 0
  for slc in itertools.product(*splits):
    if num_shards != -1:
      idx = idx % num_shards
      
    ul, lr = zip(*slc)
    ex = extent.create(ul, lr, shape)
    result[ex] = idx
    idx += 1
  
  return result
    


def create(master, shape, 
           dtype=np.float, 
           sharder=spartan.ModSharder(),
           combiner=None,
           reducer=None,
           tile_hint=None):
  dtype = np.dtype(dtype)
  shape = tuple(shape)

  table = master.create_table(sharder, combiner, reducer, TileSelector())
  extents = compute_splits(shape, tile_hint, table.num_shards())

  util.log_info('Creating array of shape %s with %d tiles', shape, len(extents))
  for ex, shard in extents.iteritems():
#     util.log_info('%s', ex)
    ex_tile = tile.from_shape(ex.shape, dtype=dtype)
    table.update(shard, ex, ex_tile)
  
  return DistArray(shape=shape, dtype=dtype, table=table, extents=extents)

empty = create


class DistArray(object):
  def __init__(self, shape, dtype, table, extents):
    self.shape = shape
    self.dtype = dtype
    self.table = table
    
    #util.log_info('%s', extents)
    Assert.isinstance(extents, dict)
    self.extents = extents
  
  def id(self):
    return self.table.id()
  
  def tile_shape(self):
    scounts = collections.defaultdict(int)
    for ex in self.extents.iterkeys():
      scounts[ex.shape] += 1
    
    return sorted(scounts.items(), key=lambda kv: kv[1])[-1][0]
    
   
  def map_to_table(self, mapper_fn, combine_fn=None, reduce_fn=None, kw=None):
    return spartan.map_items(self.table, 
                             mapper_fn = mapper_fn,
                             combine_fn = combine_fn,
                             reduce_fn = reduce_fn,
                             kw=kw)
  
  def foreach(self, fn, kw):
    return spartan.foreach(self.table, fn, kw)
  
  def __repr__(self):
    return 'DistArray(shape=%s, dtype=%s)' % (self.shape, self.dtype)
  
  def _get(self, extent):
    return self.table.get(extent)
  
  def fetch(self, region):
    '''
    Return a local numpy array for the given region.
    
    If necessary, data will be copied from remote hosts to fill the region.    
    :param region: `Extent` indicating the region to fetch.
    '''
    Assert.isinstance(region, extent.TileExtent)
    assert np.all(region.lr <= self.shape), (region, self.shape)
    
    # special case exact match against a tile 
    if region in self.extents:
      #util.log_info('Exact match.')
      ex, intersection = region, region
      shard = self.extents[region]
      return self.table.get(shard, ex)

    splits = list(extent.find_overlapping(self.extents.iterkeys(), region))
    
    #util.log_info('Target shape: %s, %d splits', region.shape, len(splits))
    tgt = np.ndarray(region.shape, dtype=self.dtype)
    for ex, intersection in splits:
      dst_slice = extent.offset_slice(region, intersection)
      shard = self.extents[ex]
      src_slice = self.table.get(shard, NestedSlice(ex, extent.offset_slice(ex, intersection)))
      #util.log_info('%s %s', dst_slice, src_slice.shape)
      tgt[dst_slice] = src_slice
    return tgt
    #return tile.data[]
   
  def update_slice(self, slc, data):
    return self.update(extent.from_slice(slc, self.shape), data)
     
  def update(self, region, data):
    Assert.isinstance(region, extent.TileExtent)
    Assert.eq(region.shape, data.shape,
              'Size of extent does not match size of data')

    #util.log_info('%s %s', self.table.id(), self.extents)
    # exact match
    if region in self.extents:
      #util.log_info('EXACT: %d %s ', self.table.id(), region)
      shard = self.extents[region]
      self.table.update(shard, region, tile.from_data(data))
      return
    
    splits = list(extent.find_overlapping(self.extents, region))
    for dst_key, intersection in splits:
      #util.log_info('%d %s %s %s', self.table.id(), region, dst_key, intersection)
      shard = self.extents[dst_key]
      src_slice = extent.offset_slice(region, intersection)
      update_tile = tile.from_intersection(dst_key, intersection, data[src_slice])
      #util.log_info('%s', update_tile)
      self.table.update(shard, dst_key, update_tile)
    
  
  def select(self, idx):
    '''
    Effectively __getitem__.
    
    Renamed to avoid the chance of accidentally using a slow, local operation on
    a distributed array.
    '''
    if isinstance(idx, extent.TileExtent):
      return self.fetch(idx)
    
    if np.isscalar(idx):
      return self[idx:idx+1][0]
    
    ex = extent.from_slice(idx, self.shape)
    return self.fetch(ex)
  
  def glom(self):
    #util.log_info('Glomming: %s', self.shape)
    return self.select(np.index_exp[:])


def from_table(table):
  '''
  Construct a distarray from an existing table.
  Keys must be of type `Extent`, values of type `Tile`.
  
  Shape is computed as the maximum range of all extents.
  
  Dtype is taken from the dtype of the tiles.
  
  :param table:
  '''
  extents = {}
  for shard, k, v in table.keys():
    extents[k] = shard
    
  Assert.no_duplicates(extents)
  
  if not extents:
    shape = tuple()
  else:
    shape = extent.find_shape(extents.keys())
  
  if len(extents) > 0:
    # fetch a one element array in order to get the dtype
    key, shard = extents.iteritems().next() 
    fetch = NestedSlice(key, np.index_exp[0:1])
    t = table.get(shard, fetch)
    # (We're not actually returning a tile, as the selector instead
    #  is returning just the underlying array.  Sigh).  
    # Assert.isinstance(t, tile.Tile)
    dtype = t.dtype
  else:
    # empty table; default dtype.
    dtype = np.float
  
  return DistArray(shape=shape, dtype=dtype, table=table, extents=extents)

def map_to_array(array, mapper_fn, combine_fn=None, reduce_fn=None, kw=None):
  return from_table(array.map_to_table(mapper_fn=mapper_fn,
                                       combine_fn=combine_fn,
                                       reduce_fn=reduce_fn,
                                       kw=kw))

  
def best_locality(array, ex):
  '''
  Return the table shard with the best locality for extent `ex`.
  :param table:
  :param ex:
  '''
  splits = extent.find_overlapping(array.extents, ex)
  counts = collections.defaultdict(int)
  for key, overlap in splits:
    shard = array.extents[key]
    counts[shard] += overlap.size
  
  s_counts = sorted(counts.items(), key=lambda kv: kv[1])
  return s_counts[-1][0]
  


def slice_mapper(ex, tile, **kw):
  '''
  Run when mapping over a slice.
  Computes the intersection of the current tile and a global slice.
  If the slice is non-zero, then run the user mapper function.
  Otherwise, do nothing.
  
  :param ex:
  :param tile: 
  :param mapper_fn: User mapper function.
  :param slice: `TileExtent` representing the slice of the input array.
  '''
  mapper_fn = kw['_slice_fn']
  slice_extent = kw['_slice_extent']
  fn_kw = kw['fn_kw']
  if fn_kw is None: fn_kw = {}
  
  intersection = extent.intersection(slice_extent, ex)
  if intersection is None:
    return []
  
  offset = extent.offset_from(slice_extent, intersection)
  
  subslice = extent.offset_slice(ex, intersection)
  subtile = tile[subslice]
  
  return mapper_fn(offset, subtile, **fn_kw)


class Slice(object):
  def __init__(self, darray, idx):
    util.log_info('New slice: %s', idx)
    if not isinstance(idx, extent.TileExtent):
      idx = extent.from_slice(idx, darray.shape)
    util.log_info('New slice: %s', idx)
    
    Assert.isinstance(darray, DistArray)
    self.darray = darray
    self.slice = idx
    self.shape = self.slice.shape
    intersections = [extent.intersection(self.slice, ex) for ex in self.darray.extents]
    intersections = [ex for ex in intersections if ex is not None]
    offsets = [extent.offset_from(self.slice, ex) for ex in intersections]
    self.extents = offsets
    self.dtype = darray.dtype
    
  def foreach(self, mapper_fn, kw):
    return spartan.foreach(self.darray.table,
                           fn=slice_mapper,
                           kw={'_slice_extent' : self.slice,
                               '_slice_fn' : mapper_fn,
                               'fn_kw' : kw })
  
  def map_to_table(self, mapper_fn, kw, combine_fn=None, reduce_fn=None):
    return spartan.map_items(self.darray.table, 
                             mapper_fn = slice_mapper,
                             combine_fn = combine_fn,
                             reduce_fn = reduce_fn,
                             kw={'fn_kw' : kw,
                                 '_slice_extent' : self.slice,
                                 '_slice_fn' : mapper_fn })
    
  def fetch(self, idx):
    offset = extent.compute_slice(self.slice, idx.to_slice())
    return self.darray.fetch(offset)

  def glom(self):
    return self.darray.fetch(self.slice)

  def __getitem__(self, idx):
    ex = extent.compute_slice(self.slice, idx)
    return self.darray.fetch(ex)


def broadcast_mapper(ex, tile, mapper_fn=None, bcast_obj=None):
  pass


class Broadcast(object):
  '''A broadcast object mimics the behavior of Numpy broadcasting.
  
  Takes an input of shape (x, y) and a desired output shape (x, y, z),
  the broadcast object reports shape=(x,y,z) and overrides __getitem__
  to return the appropriate values.
  '''
  def __init__(self, base, shape):
    Assert.isinstance(base, (np.ndarray, DistArray))
    Assert.isinstance(shape, tuple)
    self.base = base
    self.shape = shape
  
  def __repr__(self):
    return 'Broadcast(%s -> %s)' % (self.base, self.shape)
  
  def fetch(self, ex):
    # drop extra dimensions
    while len(ex.shape) > len(self.base.shape):
      ex = extent.drop_axis(ex, -1)
      
    # fold down expanded dimensions
    ul = []
    lr = []
    for i in xrange(len(self.base.shape)):
      size = self.base.shape[i]
      if size == 1:
        ul.append(0)
        lr.append(1)
      else:
        ul.append(ex.ul[i])
        lr.append(ex.lr[i])
    
    ex = extent.create(ul, lr, self.base.shape) 
   
    template = np.ndarray(ex.shape, dtype=self.base.dtype)
    fetched = self.base.fetch(ex)
    
    _, bcast = np.broadcast_arrays(template, fetched)
    return bcast 
  
  def map_to_table(self, mapper_fn, combine_fn=None, reduce_fn=None, kw=None):
    raise NotImplementedError
  
  def foreach(self, fn):
    raise NotImplementedError
  

def broadcast(args):
  if len(args) == 1:
    return args
 
  orig_shapes = [list(x.shape) for x in args]
  dims = [len(shape) for shape in orig_shapes]
  max_dim = max(dims)
  new_shapes = []
  
  # prepend filler dimensions for smaller arrays
  for i in range(len(orig_shapes)):
    diff = max_dim - len(orig_shapes[i])
    new_shapes.append([1] * diff + orig_shapes[i])
 
  # check shapes are valid
  # for each axis, all arrays should either share the 
  # same size, or have size == 1
  for axis in range(max_dim):
    axis_shape = set(s[axis] for s in new_shapes)
   
    assert len(axis_shape) <= 2, 'Mismatched shapes for broadcast: %s' % orig_shapes
    if len(axis_shape) == 2:
      assert 1 in axis_shape, 'Mismatched shapes for broadcast: %s' % orig_shapes
  
    # now lift the inputs with size(axis) == 1 
    # to have the maximum size for the axis 
    max_size = max(s[axis] for s in new_shapes)
    for s in new_shapes:
      s[axis] = max_size
    
  # wrap arguments with missing dims in a Broadcast object.
  results = []
  for i in range(len(args)):
    if new_shapes[i] == orig_shapes[i]:
      results.append(args[i])
    else:
      results.append(Broadcast(args[i], tuple(new_shapes[i])))
    
  #util.log_debug('Broadcast result: %s', results)
  return results

def _size(v):
  if isinstance(v, Broadcast):
    return np.prod(v.base.shape)
  return np.prod(v.shape)

def largest_value(vals):
  return sorted(vals, key=lambda v: _size(v))[-1]

