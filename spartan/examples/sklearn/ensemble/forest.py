import numpy as np
import spartan
from spartan import expr, core, blob_ctx, array, util
from sklearn.ensemble import RandomForestClassifier as SKRF
import time


def _build_mapper(ex,
                  task_array,
                  target_array,
                  X,
                  y,
                  criterion,
                  max_depth,
                  min_samples_split,
                  min_samples_leaf,
                  max_features,
                  bootstrap):
  """
  Mapper kernel for building a random forest classifier.

  Each kernel instance fetches the entirety of the feature and prediction
  (X and y) arrays, and invokes sklearn to create a local random forest classifier
  which may has more than one tree.

  The criterion, max_depth, min_samples_split, min_samples_leaf,
  max_features and bootstrap options are passed to the `sklearn.RandomForest` method.
  """
  # The number of rows decides how many trees this kernel will build.
  st = time.time()
  idx = ex.ul[0]
  # Get the number of trees this worker needs to train.
  n_estimators = task_array[idx]
  X = X.glom()
  y = y.glom()

  rf = SKRF(n_estimators = n_estimators,
                           criterion = criterion,
                           max_depth = max_depth,
                           n_jobs = 1,
                           min_samples_split = min_samples_split,
                           min_samples_leaf = min_samples_leaf,
                           max_features = max_features,
                           bootstrap = bootstrap)

  rf.fit(X, y)
  # Update the target array.
  target_array[idx, :] = (rf,)

  result = core.LocalKernelResult()
  result.result = None
  util.log_info("Finish construction : %s", time.time() - st)
  return result


def _predict_mapper(ex,
                    forest_array,
                    X):
  """
  Predict kernel:
  Each worker uses the sklearn random forest it generates to predict labels.
  Return the probilities of the predicted labels to master.
  """
  idx = ex.ul[0]
  forest = forest_array[idx]

  proba = forest.predict_proba(X) * len(forest.estimators_)

  result = core.LocalKernelResult()
  result.result = proba
  return result


class RandomForestClassifier(object):
  """A random forest classifier.

  A random forest is a meta estimator that fits a number of decision tree
  classifiers on various sub-samples of the dataset and use averaging to
  improve the predictive accuracy and control over-fitting.

  Parameters
  ----------
  n_estimators : integer, optional (default=10)
      The number of trees in the forest.

  criterion : string, optional (default="gini")
      The function to measure the quality of a split. Supported criteria are
      "gini" for the Gini impurity and "entropy" for the information gain.
      Note: this parameter is tree-specific.

  max_features : int, float, string or None, optional (default="auto")
      The number of features to consider when looking for the best split:
        - If int, then consider `max_features` features at each split.
        - If float, then `max_features` is a percentage and
          `int(max_features * n_features)` features are considered at each
          split.
        - If "auto", then `max_features=sqrt(n_features)`.
        - If "sqrt", then `max_features=sqrt(n_features)`.
        - If "log2", then `max_features=log2(n_features)`.
        - If None, then `max_features=n_features`.

      Note: this parameter is tree-specific.

  max_depth : integer or None, optional (default=None)
      The maximum depth of the tree. If None, then nodes are expanded until
      all leaves are pure or until all leaves contain less than
      min_samples_split samples.
      Ignored if ``max_samples_leaf`` is not None.
      Note: this parameter is tree-specific.

  min_samples_split : integer, optional (default=2)
      The minimum number of samples required to split an internal node.
      Note: this parameter is tree-specific.

  min_samples_leaf : integer, optional (default=1)
      The minimum number of samples in newly created leaves.  A split is
      discarded if after the split, one of the leaves would contain less then
      ``min_samples_leaf`` samples.
      Note: this parameter is tree-specific.

  max_leaf_nodes : int or None, optional (default=None)
      Grow trees with ``max_leaf_nodes`` in best-first fashion.
      Best nodes are defined as relative reduction in impurity.
      If None then unlimited number of leaf nodes.
      If not None then ``max_depth`` will be ignored.
      Note: this parameter is tree-specific.

  bootstrap : boolean, optional (default=True)
      Whether bootstrap samples are used when building trees.
  """
  def __init__(self,
               n_estimators=10,
               criterion="gini",
               max_depth=None,
               min_samples_split=2,
               min_samples_leaf=1,
               max_features="auto",
               max_leaf_nodes=None,
               bootstrap=True):
    self.n_estimators = n_estimators
    self.criterion = criterion
    self.max_depth = max_depth
    self.min_samples_split = min_samples_split
    self.min_samples_leaf = min_samples_leaf
    self.max_features = max_features
    self.max_leaf_nodes = max_leaf_nodes
    self.bootstrap = bootstrap
    self.forests = None

  def _create_task_array(self, n_workers, n_trees):
    """
    Construct the task array. Tells the worker how many trees they need to build.
    For example,
    if task_array[2] == 10, this means the third worker needs to build 10 trees.
    """
    n_workers = int(n_workers)
    n_trees = int(n_trees)
    if n_trees <= n_workers:
      return np.ones(n_trees, dtype=np.int)

    task_array = np.empty(n_workers, dtype=np.int)
    n_trees_per_worker = n_trees / n_workers
    n_trees_for_last_worker = n_trees % n_workers + n_trees_per_worker

    for i in range(n_workers):
      if i == n_workers - 1:
        task_array[i] = n_trees_for_last_worker
      else:
        task_array[i] = n_trees_per_worker
    return task_array

  def fit(self, X, y):
    """
    Parameters
    ----------
    X : array-like of shape = [n_samples, n_features]
        The training input samples.

    y : array-like, shape = [n_samples] or [n_samples, n_outputs]
        The target values (integers that correspond to classes in
        classification, real numbers in regression).

    Returns
    -------
    self : object
        Returns self.
    """
    if isinstance(X, np.ndarray):
      X = expr.from_numpy(X)
    if isinstance(y, np.ndarray):
      y = expr.from_numpy(y)

    X = X.evaluate()
    y = y.evaluate()

    self.n_classes = np.unique(y.glom()).size
    ctx = blob_ctx.get()
    n_workers = ctx.num_workers

    _ = self._create_task_array(n_workers, self.n_estimators)
    task_array = expr.from_numpy(_, tile_hint=(1, )).evaluate()
    target_array = expr.ndarray((task_array.shape[0], ), dtype=object, tile_hint=(1,)).evaluate()

    results = task_array.foreach_tile(mapper_fn=_build_mapper,
                                      kw={'task_array': task_array,
                                          'target_array': target_array,
                                          'X': X,
                                          'y': y,
                                          'criterion': self.criterion,
                                          'max_depth': self.max_depth,
                                          'min_samples_split': self.min_samples_split,
                                          'min_samples_leaf': self.min_samples_leaf,
                                          'max_features': self.max_features,
                                          'bootstrap': self.bootstrap})

    # Target array stores the local random forest each worker builds,
    # it's used for further prediction.
    self.target_array = target_array
    return self

  def predict(self, X):
    """
    Parameters
    ----------
    X : array-like of shape = [n_samples, n_features]

    Returns
    -------
    Y : numpy array of shape = [n_samples,]
        The predicted label of X.
    """
    if isinstance(X, expr.Expr) or isinstance(X, array.distarray.DistArray):
      X = X.glom()

    results = self.target_array.foreach_tile(mapper_fn=_predict_mapper,
                                             kw={'forest_array': self.target_array,
                                                 'X': X})

    probas = np.zeros((X.shape[0], self.n_classes), np.float64)
    for k, v in results.iteritems():
      probas += v

    # Choose the most probably one.
    result = np.array([np.argmax(probas[i]) for i in xrange(probas.shape[0])])
    return result

  def score(self, X, y):
    """Return the mean accuracy on the given test data and labels.

    Parameters
    ----------
    X : array-like, shape = (n_samples, n_features)
        Test samples.

    y : array-like, shape = (n_samples,)
        True labels for X.

    Returns
    -------
    score : float
        Mean accuracy of self.predict(X) wrt. y.
    """
    if not isinstance(y, np.ndarray):
      y = y.glom()
    return np.mean(self.predict(X) == y)
