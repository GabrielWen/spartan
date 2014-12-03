'''
Reshape operation and expr.
'''

import itertools
import numpy as np
import scipy.sparse as sp
from spartan import rpc
from .base import Expr, lazify
from .. import master, blob_ctx, util
from ..util import is_iterable, Assert
from ..array import extent, distarray
from ..core import LocalKernelResult
from .shuffle import target_mapper
from traits.api import PythonValue, Instance, Tuple
import time

from .retile import retile as retile_force

def _ravelled_ex(ul, lr, shape):
  ravelled_ul = extent.ravelled_pos(ul, shape)
  ravelled_lr = extent.ravelled_pos([l - 1 for l in lr], shape)
  return ravelled_ul, ravelled_lr


def _unravelled_ex(ravelled_ul, ravelled_lr, shape):
  ul = extent.unravelled_pos(ravelled_ul, shape)
  lr = extent.unravelled_pos(ravelled_lr, shape)
  return ul, lr


def _tile_mapper(tile_id, blob, array=None, user_fn=None, **kw):
  if array.shape_array is None:
    # Maps over the original array, translating the region to reflect the
    # reshape operation.
    ex = array.base.extent_for_blob(tile_id)
    ravelled_ul, ravelled_lr = _ravelled_ex(ex.ul, ex.lr, array.base.shape)
    unravelled_ul, unravelled_lr = _unravelled_ex(ravelled_ul,
                                                  ravelled_lr,
                                                  array.shape)
    ex = extent.create(unravelled_ul, np.array(unravelled_lr) + 1, array.shape)
  else:
    ex = array.shape_array.extent_for_blob(tile_id)
  return user_fn(ex, **kw)


def _fetch_subarray(tile, ex_shape, array_shape):
  '''
  Fetch a subarray from tile with given shape

  param:
    1. tile: vector
          To be noted, we fetch from the first element of this input.
          Therefore, do remember to set up the value appropriately.

    2. ex_shape: tuple
          Expected shape of output

    3. array_shape: tuple
          Array's shape currently

  Return: list
    Returns array in list data type as expected
  '''
  if len(ex_shape) == 1:
    return tile[:ex_shape[0]].tolist()

  ret = []

  #Compute the distance we need to jump during each recursion
  step = np.prod(array_shape[1:])

  for i in xrange(ex_shape[0]):
    ret.append(_fetch_subarray(tile[i*step:], ex_shape[1:], array_shape[1:]))

  return ret

def _colfetch_slice(start, ex_shape, array_shape):
  '''
  Compute slices this fetch needs

  param:
    1. start: int
	  Starting point of slices

    2. ex_shape: tuple
	  Expected shape of output

    3. array_shape: tuple
	  Array's shape currently

  Return: list
    Returns a list that contains slices this fetch
    operation needs
  '''
  if len(ex_shape) == 1:
    return [slice(start, start + ex_shape[0], None)]

  ret = []

  step = np.prod(array_shape[1:])

  for i in xrange(ex_shape[0]):
    ret.extend(_colfetch_slice(start + (i*step), ex_shape[1:], array_shape[1:]))

  return ret

class Reshape(distarray.DistArray):
  '''Reshape the underlying array base.

  Reshape does not create a copy of the base array. Instead the fetch method is
  overridden:
  1. Caculate the underlying extent containing the requested extent.
  2. Fetch the underlying extent.
  3. Trim the fetched tile and reshape to the requested tile.

  To support foreach_tile() and tile_shape() (used by dot), Reshape needs an
  blob_id-to-extent map and extents shape. Therefore, Reshape creates a
  distarray (shape_array), but Reshape doesn't initialize its content.
  '''

  def __init__(self, base, shape, tile_hint=None):
    Assert.isinstance(base, distarray.DistArray)
    self.base = base
    self.shape = shape
    self.dtype = base.dtype
    self.sparse = self.base.sparse
    self.tiles = self.base.tiles
    self.bad_tiles = []
    self._tile_shape = distarray.good_tile_shape(shape,
                                                 master.get().num_workers)
    self.shape_array = None

    # Check the special case which is add a new dimension.
    self.is_add_dimension = False
    if len(shape) == len(self.base.shape) + 1:
      self.is_add_dimension = True
      extra = 0
      for i in range(len(self.base.shape)):
        if shape[i + extra] != self.base.shape[i]:
          if extra == 0 and shape[i] == 1:
            self.new_dimension_idx = i
            extra = 1
          else:
            self.is_add_dimension = False
            break
      if extra == 0:
        self.new_dimension_idx = len(shape) - 1

    self._check_extents()

  def _check_extents(self):
    ''' Check if original extents are still rectangles after reshaping.

    If original extents are still rectangles after reshaping, _check_extents
    sets _same_tiles to avoid creating a new distarray in foreach_tile().
    '''

    self._same_tiles = True
    # Special cases, check if we just add an extra dimension.
    if len(self.shape) > len(self.base.shape):
      for i in range(len(self.base.shape)):
        if self.base.shape[i] != self.shape[i]:
          self._same_tiles = False
          break
      if self._same_tiles:
        return

    # For each (ul, lr) in the new matrix, _compute_split checks if they can
    # form a retangle in the original matrix.
    splits = distarray.compute_splits(self.shape, self._tile_shape)

    for slc in itertools.product(*splits):
      ul, lr = zip(*slc)
      ravelled_ul, ravelled_lr = _ravelled_ex(ul, lr, self.shape)
      rect_ul, rect_lr = extent.find_rect(ravelled_ul, ravelled_lr, self.base.shape)
      if rect_ul or ul or rect_lr != lr:
        self._same_tiles = False
        break

  def tile_shape(self):
    return self._tile_shape

  def foreach_tile(self, mapper_fn, kw=None):
    if kw is None: kw = {}
    kw['array'] = self
    kw['user_fn'] = mapper_fn

    assert getattr(self.base, 'tiles', None) is not None, "Reshape's base must have tiles"
    if self._same_tiles:
      tiles = self.base.tiles.values()
    else:
      if self.shape_array is None:
        self.shape_array = distarray.create(self.shape, self.base.dtype,
                                            tile_hint=self._tile_shape,
                                            sparse=self.base.sparse)
      tiles = self.shape_array.tiles.values()

    return blob_ctx.get().map(tiles, mapper_fn=_tile_mapper, kw=kw)

  def extent_for_blob(self, id):
    base_ex = self.base.blob_to_ex[id]
    ravelled_ul, ravelled_lr = _ravelled_ex(base_ex.ul, base_ex.lr, self.base.shape)
    unravelled_ul, unravelled_lr = _unravelled_ex(ravelled_ul,
                                                  ravelled_lr,
                                                  self.shape)
    return extent.create(unravelled_ul, np.array(unravelled_lr) + 1, self.shape)

  def col_fetch(self, ex):
    '''
    Handler for fetch of segmented data
    '''
    util.log_info('Evaluating Column Fetch: %s', ex)
    if isinstance(ex, extent.TileExtent):
      start, stop = _ravelled_ex(ex.ul, ex.lr, self.shape)
      time_start = time.time()
      ravelled_subslice = _colfetch_slice(start, ex.shape, self.shape)
      util.log_info('\nTIME _colfetch_slice: %s\n', time.time() - time_start)
    else:
      ravelled_subslice = ex

    #if isinstance(ravelled_subslice, list):
    #  time_start = time.time()
    #  ravelled_subslice = [x for x in util.flatten_list(ravelled_subslice, slice)]
    #  util.log_info('\nTIME util.flatten_list: %s\n', time.time() - time_start)

    return self.base.col_fetch(ravelled_subslice).reshape(ex.shape)

  def fetch(self, ex, seg = False):
    if self.is_add_dimension:
      ul = ex.ul[0:self.new_dimension_idx] + ex.ul[self.new_dimension_idx + 1:]
      lr = ex.lr[0:self.new_dimension_idx] + ex.lr[self.new_dimension_idx + 1:]
      base_ex = extent.create(ul, lr, self.base.shape)
      return self.base.fetch(base_ex).reshape(ex.shape)

    # TODO : Following code can't handle `column fetch`. Since it assume
    #        the base region being fetched is continous. But it is not
    #        true when the `ex` doesn't contain complete rows.
    ravelled_ul, ravelled_lr = _ravelled_ex(ex.ul, ex.lr, self.shape)
    base_ravelled_ul, base_ravelled_lr = extent.find_rect(ravelled_ul,
                                                          ravelled_lr,
                                                          self.base.shape)
    base_ul, base_lr = _unravelled_ex(base_ravelled_ul,
                                      base_ravelled_lr,
                                      self.base.shape)
    base_ex = extent.create(base_ul, np.array(base_lr) + 1, self.base.shape)

    util.log_info('self.base: tile_hint: %s shape: %s', self.base.tile_shape(), self.base.shape)

    util.log_info('base.shape: %s', self.base.tile_shape())

    new_shape = [1]
    new_shape.extend(list(self.base.shape[1:]))

    util.log_info('self.base: %s => %s', self.base, tuple(new_shape))

    tile = self.base.fetch(base_ex)
    
    if len(self.shape) == 1 or base_ul[:-1] == base_lr[:-1]:
      cont = True
    else:
      cont = False
    

    if not self.base.sparse:
      tile = np.ravel(tile)
      if cont:
	#For continuous data fetch
        tile = tile[(ravelled_ul - base_ravelled_ul):(ravelled_lr - base_ravelled_ul) + 1]
      else:
	#For segmented data fetch
        tile = np.array(_fetch_subarray(tile[ravelled_ul - base_ravelled_ul:], ex.shape, self.shape))

      assert np.prod(tile.shape) == np.prod(ex.shape), (tile.shape, ex.shape)

      return tile.reshape(ex.shape)
    else:
      tile = tile.tolil()
      new = sp.lil_matrix(ex.shape, dtype=self.base.dtype)
      j_max = tile.shape[1]
      for i, row in enumerate(tile.rows):
        for col, j in enumerate(row):
          rect_index = i*j_max + j
          target_start = base_ravelled_ul - ravelled_ul
          target_end = base_ravelled_lr - ravelled_ul
          if rect_index >= target_start and rect_index <= target_end:
            new_r, new_c = np.unravel_index(rect_index - target_start, ex.shape)
            new[new_r, new_c] = tile[i, j]
      return new


class ReshapeExpr(Expr):
  array = Instance(Expr)
  new_shape = Tuple
  tile_hint = PythonValue(None, desc="None or Tuple")
  dimension_update = False

  def __str__(self):
    return 'Reshape[%d] %s to %s' % (self.expr_id, self.array, self.new_shape)

  def _evaluate(self, ctx, deps):
    v = deps['array']
    shape = deps['new_shape']
    
    util.log_info('v: %s %s -> %s', v, v.shape, self.new_shape)

    util.log_info('dimension_update: %s', self.dimension_update)

    if not self.dimension_update and len(v.shape) > 1 and not np.prod(v.tile_shape()[1:]) == np.prod(v.shape[1:]):
      util.log_info('retiling...')
      new_tile = [v.shape[0] / blob_ctx.get().num_workers] + list(v.shape[1:])
      v = retile_force(v, tuple(new_tile)).force()

    return Reshape(v, shape, self.tile_hint)

  def compute_shape(self):
    return self.new_shape


def reshape(array, new_shape, tile_hint=None):
  '''
  Reshape/retile ``array``.

  Args:
    array : `Expr` to reshape.
    new_shape (tuple): Target shape.
    tile_hint (tuple):

  Returns:
    `ReshapeExpr`: Reshaped array.
  '''
  Assert.isinstance(new_shape, tuple)
  array = lazify(array)

  return ReshapeExpr(array=array,
                     new_shape=new_shape,
                     tile_hint=tile_hint)


def retile(array, tile_hint):
  '''
  Change the tiling of ``array``, while retaining the same shape.

  Args:
    array(Expr): Array to reshape
    tile_hint(tuple): New tile shape
  '''
  return reshape(array, array.shape, tile_hint)
