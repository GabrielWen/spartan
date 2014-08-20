import numpy as np
import spartan
from spartan import expr
from spartan import util

class SGDRegressor(object):
  '''Stochastic gradient descent algorithm

  SGDRegressor uses stochastic gradient descent algorithm to approach
  the best W(parameters) for regressions.

  Formula : W = W - alpha * gradient_update
  '''
  def __init__(self, x, y, iterations, alpha = 1e-6):
    '''
    SGDRegressor try to approach the best w for y = wx.

    Args
      x: `Expr`
      y: `Expr`
      iterations: An integer indicating the learning iterations for SGD.
      alpha: An integer indicating the learning rate for SGD
    '''
    self.x = x
    self.y = y
    self.iterations = iterations
    self.alpha = alpha
    self.N_DIM = self.x.shape[1]
    self.w = np.random.rand(self.N_DIM, 1)

  def update(self):
    raise NotImplementedError("Should be overrided by the child regression")

  def train(self):
    for i in range(self.iterations):
      diff = self.update()
      grad = expr.sum(diff, axis=0).optimized().glom().reshape((self.N_DIM, 1))
      self.w = self.w - grad * self.alpha
    return self.w

