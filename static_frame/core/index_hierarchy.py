
import typing as tp
from collections.abc import KeysView
from itertools import chain
from ast import literal_eval


import numpy as np

from static_frame.core.container import ContainerOperand

from static_frame.core.util import DEFAULT_SORT_KIND

from static_frame.core.index_base import IndexBase
from static_frame.core.index import Index
from static_frame.core.index import IndexGO
from static_frame.core.index import _requires_reindex
from static_frame.core.index import mutable_immutable_index_filter

from static_frame.core.util import IndexConstructor
from static_frame.core.util import IndexConstructors
from static_frame.core.util import GetItemKeyType
from static_frame.core.util import intersect2d
from static_frame.core.util import union2d
from static_frame.core.util import setdiff2d
from static_frame.core.util import name_filter
from static_frame.core.util import isin
from static_frame.core.util import iterable_to_array_2d
from static_frame.core.util import INT_TYPES
from static_frame.core.util import NameType


from static_frame.core.selector_node import InterfaceGetItem
from static_frame.core.selector_node import InterfaceAsType


from static_frame.core.util import CallableOrMapping
from static_frame.core.util import DepthLevelSpecifier


from static_frame.core.container_util import matmul
from static_frame.core.container_util import index_from_optional_constructor
from static_frame.core.container_util import rehierarch_and_map

from static_frame.core.array_go import ArrayGO
from static_frame.core.type_blocks import TypeBlocks

from static_frame.core.display import DisplayConfig
from static_frame.core.display import DisplayActive
from static_frame.core.display import Display
from static_frame.core.display import DisplayHeader

from static_frame.core.iter_node import IterNodeType
from static_frame.core.iter_node import IterNodeDepthLevel
from static_frame.core.iter_node import IterNodeApplyType

from static_frame.core.hloc import HLoc

from static_frame.core.index_level import IndexLevel

from static_frame.core.index_level import IndexLevelGO
from static_frame.core.exception import ErrorInitIndex
from static_frame.core.doc_str import doc_inject

if tp.TYPE_CHECKING:

    from pandas import DataFrame #pylint: disable=W0611 #pragma: no cover
    from static_frame.core.frame import Frame #pylint: disable=W0611 #pragma: no cover
    from static_frame.core.frame import FrameGO #pylint: disable=W0611 #pragma: no cover


IH = tp.TypeVar('IH', bound='IndexHierarchy')

CONTINUATION_TOKEN_INACTIVE = object()

#-------------------------------------------------------------------------------
class IndexHierarchy(IndexBase):
    '''
    A hierarchy of :obj:`static_frame.Index` objects, defined as strict tree of uniform depth across all branches.
    '''
    __slots__ = (
            '_levels',
            '_blocks',
            '_recache',
            '_name',
            )
    _levels: IndexLevel
    _blocks: TypeBlocks
    _recache: bool
    _name: tp.Hashable
    _keys: KeysView

    # Temporary type overrides, until indices are generic.
    __getitem__: tp.Callable[['IndexHierarchy', tp.Hashable], tp.Tuple[tp.Hashable, ...]]
    # __iter__: tp.Callable[['IndexHierarchy'], tp.Iterator[tp.Tuple[tp.Hashable, ...]]]
    # __reversed__: tp.Callable[['IndexHierarchy'], tp.Iterator[tp.Tuple[tp.Hashable, ...]]]

    # _IMMUTABLE_CONSTRUCTOR is None from IndexBase
    # _MUTABLE_CONSTRUCTOR will be defined after IndexHierarhcyGO defined

    _INDEX_CONSTRUCTOR = Index
    _LEVEL_CONSTRUCTOR = IndexLevel

    _UFUNC_UNION = union2d
    _UFUNC_INTERSECTION = intersect2d
    _UFUNC_DIFFERENCE = setdiff2d

    _NDIM: int = 2


    #---------------------------------------------------------------------------
    # constructors

    @classmethod
    def from_product(cls: tp.Type[IH],
            *levels,
            name: NameType = None
            ) -> IH:
        '''
        Given groups of iterables, return an ``IndexHierarchy`` made of the product of a values in those groups, where the first group is the top-most hierarchy.

        Returns:
            :obj:`static_frame.IndexHierarchy`

        '''
        indices = [] # store in a list, where index is depth
        for lvl in levels:
            if not isinstance(lvl, Index): # Index, not IndexBase
                lvl = cls._INDEX_CONSTRUCTOR(lvl)
            indices.append(lvl)

        if len(indices) == 1:
            raise RuntimeError('only one level given')

        # build name from index names, assuming they are all specified
        if name is None:
            name = tuple(index.name for index in indices)
            if any(n is None for n in name):
                name = None

        targets_previous = None

        # need to walk up from bottom to top
        # get depth pairs and iterate over those
        depth = len(indices) - 1
        while depth > 0:
            index = indices[depth]
            index_up = indices[depth - 1]
            # for each label in the next-up index, we need a reference to this index with an offset of that index (or level)
            targets = np.empty(len(index_up), dtype=object)

            offset = 0
            for idx, _ in enumerate(index_up):
                # this level does not have targets, only an index (as a leaf)
                level = cls._LEVEL_CONSTRUCTOR(index=index,
                        offset=offset,
                        targets=targets_previous)

                targets[idx] = level
                offset += len(level)
            targets_previous = ArrayGO(targets, own_iterable=True)
            depth -= 1

        level = cls._LEVEL_CONSTRUCTOR(index=index_up, targets=targets_previous)
        return cls(level, name=name)

    @classmethod
    def _tree_to_index_level(cls,
            tree,
            index_constructors: tp.Optional[IndexConstructors] = None
            ) -> IndexLevel:
        '''
        Convert a tree structure to an IndexLevel instance.
        '''
        # tree: tp.Dict[tp.Hashable, tp.Union[Sequence[tp.Hashable], tp.Dict]]

        def get_index(labels, depth: int):
            if index_constructors is not None:
                explicit_constructor = index_constructors[depth]
            else:
                explicit_constructor = None

            return index_from_optional_constructor(labels,
                    default_constructor=cls._INDEX_CONSTRUCTOR,
                    explicit_constructor=explicit_constructor)

        def get_level(level_data, offset=0, depth=0):

            if isinstance(level_data, dict):
                level_labels = []
                targets = np.empty(len(level_data), dtype=object)
                offset_local = 0

                # ordered key, value pairs, where the key is the label, the value is a list or dictionary; enmerate for insertion pre-allocated object array
                for idx, (k, v) in enumerate(level_data.items()):
                    level_labels.append(k)
                    level = get_level(v, offset=offset_local, depth=depth + 1)
                    targets[idx] = level
                    offset_local += len(level) # for lower level offsetting

                index = get_index(level_labels, depth=depth)
                targets = ArrayGO(targets, own_iterable=True)

            else: # an iterable, terminal node, no offsets needed
                index = get_index(level_data, depth=depth)
                targets = None

            return cls._LEVEL_CONSTRUCTOR(
                    index=index,
                    offset=offset,
                    targets=targets,
                    )

        return get_level(tree)


    @classmethod
    def from_tree(cls: tp.Type[IH],
            tree,
            *,
            name: tp.Hashable = None
            ) -> IH:
        '''
        Convert into a ``IndexHierarchy`` a dictionary defining keys to either iterables or nested dictionaries of the same.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        return cls(cls._tree_to_index_level(tree), name=name)


    @classmethod
    def from_labels(cls: tp.Type[IH],
            labels: tp.Iterable[tp.Sequence[tp.Hashable]],
            *,
            name: NameType = None,
            reorder_for_hierarchy: bool = False,
            index_constructors: tp.Optional[IndexConstructors] = None,
            continuation_token: tp.Union[tp.Hashable, None] = CONTINUATION_TOKEN_INACTIVE
            ) -> IH:
        '''
        Construct an ``IndexHierarhcy`` from an iterable of labels, where each label is tuple defining the component labels for all hierarchies.

        Args:
            labels: an iterator or generator of tuples.
            reorder_for_hierarchy: reorder the labels to produce a hierarchible Index, assuming hierarchability is possible.
            continuation_token: a Hashable that will be used as a token to identify when a value in a label should use the previously encountered value at the same depth.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        if reorder_for_hierarchy:
            if continuation_token != CONTINUATION_TOKEN_INACTIVE:
                raise RuntimeError('continuation_token not supported when reorder_for_hiearchy')
            # we need a single numpy array to use rehierarch_and_map
            index_labels = iterable_to_array_2d(labels)
            # this will reorder and create the index using this smae method, passed as cls.from_labels
            index, _ = rehierarch_and_map(
                    labels=index_labels,
                    depth_map=range(index_labels.shape[1]), # keep order
                    index_constructor=cls.from_labels,
                    index_constructors=index_constructors,
                    name=name,
                    )
            return index

        labels_iter = iter(labels)
        try:
            first = next(labels_iter)
        except StopIteration:
            # if iterable is empty, return empty index
            return cls(levels=cls._LEVEL_CONSTRUCTOR(
                    cls._INDEX_CONSTRUCTOR(())
                    ), name=name)

        depth = len(first)
        # minimum permitted depth is 2
        if depth < 2:
            raise ErrorInitIndex('cannot create an IndexHierarchy from only one level.')
        if index_constructors and len(index_constructors) != depth:
            raise ErrorInitIndex('if providing index constructors, number of index constructors must equal depth of IndexHierarchy.')

        depth_max = depth - 1
        depth_pre_max = depth - 2

        token = object()
        observed_last = [token for _ in range(depth)]

        tree = dict() # order assumed and necessary
        # put first back in front
        for label in chain((first,), labels_iter):
            current = tree # NOTE: over the life of this loop, current can be a dict or a list
            # each label is an iterable
            for d, v in enumerate(label):
                # print('d', d, 'v', v, 'depth_pre_max', depth_pre_max, 'depth_max', depth_max)
                if continuation_token is not CONTINUATION_TOKEN_INACTIVE:
                    if v == continuation_token:
                        # might check that observed_last[d] != token
                        v = observed_last[d]
                if d < depth_pre_max:
                    if v not in current:
                        current[v] = dict() # order necessary
                    else:
                        # can only fetch this node (and not create a new node) if this is the sequential predecessor
                        if v != observed_last[d]:
                            raise ErrorInitIndex(f'invalid tree-form for IndexHierarchy: {v} in {label} cannot follow {observed_last[d]} when {v} has already been defined.')
                    current = current[v]
                    observed_last[d] = v
                elif d < depth_max:
                    if v not in current:
                        current[v] = list()
                    else:
                        # cannot just fetch this list if it is not the predecessor
                        if v != observed_last[d]:
                            raise ErrorInitIndex(f'invalid tree-form for IndexHierarchy: {v} in {label} cannot follow {observed_last[d]} when {v} has already been defined.')
                    current = current[v]
                    observed_last[d] = v
                elif d == depth_max: # at depth max
                    # if there are redundancies here they will be caught in index creation
                    current.append(v)
                else:
                    raise ErrorInitIndex('label exceeded expected depth', label)

        levels = cls._tree_to_index_level(
                tree,
                index_constructors=index_constructors
                )
        return cls(levels=levels, name=name)

    @classmethod
    def from_index_items(cls: tp.Type[IH],
            items: tp.Iterable[tp.Tuple[tp.Hashable, Index]],
            *,
            index_constructor: tp.Optional[IndexConstructor] = None
            ) -> IH:
        '''
        Given an iterable of pairs of label, :obj:`Index`, produce an :obj:`IndexHierarchy` where the labels are depth 0, the indices are depth 1.

        Args:
            items: iterable of pairs of label, :obj:`Index`.
            index_constructor: Optionally provide index constructor for outermost index.
        '''
        labels = []
        index_levels = []

        offset = 0
        for label, index in items:
            labels.append(label)

            index = mutable_immutable_index_filter(cls.STATIC, index)
            index_levels.append(cls._LEVEL_CONSTRUCTOR(
                    index,
                    offset=offset,
                    own_index=True)
            )
            offset += len(index)

        targets = ArrayGO(np.array(index_levels, dtype=object), own_iterable=True)

        index_outer = index_from_optional_constructor(labels,
                    default_constructor=cls._INDEX_CONSTRUCTOR,
                    explicit_constructor=index_constructor)

        return cls(cls._LEVEL_CONSTRUCTOR(
                index=index_outer,
                targets=targets,
                own_index=True
                ))


    @classmethod
    def from_labels_delimited(cls: tp.Type[IH],
            labels: tp.Iterable[str],
            *,
            delimiter: str = ' ',
            name: NameType = None,
            index_constructors: tp.Optional[IndexConstructors] = None,
            ) -> IH:
        '''
        Construct an ``IndexHierarhcy`` from an iterable of labels, where each label is string defining the component labels for all hierarchies using a string delimiter. All components after splitting the string by the delimited will be literal evaled to produce proper types; thus, strings must be quoted.

        Args:
            labels: an iterator or generator of tuples.

        Returns:
            :obj:`static_frame.IndexHierarchy`
        '''
        def trim_outer(label: str) -> str:
            start, stop = 0, len(label)
            if label[0] in ('[', '('):
                start = 1
            if label[-1] in (']', ')'):
                stop = -1
            return label[start: stop]

        labels = (tuple(literal_eval(x)
                for x in trim_outer(label).split(delimiter))
                for label in labels
                )
        return cls.from_labels(labels,
                name=name,
                index_constructors=index_constructors
                )


    @classmethod
    def _from_type_blocks(cls: tp.Type[IH],
            blocks: TypeBlocks,
            *,
            name: NameType = None,
            index_constructors: tp.Optional[IndexConstructors] = None,
            own_blocks: bool = False,
            ) -> IH:
        '''
        Construct an :obj:`IndexHierarchy` from a :obj:`TypeBlocks` instance.

        Args:
            blocks: a TypeBlocks instance

        Returns:
            :obj:`IndexHierarchy`
        '''

        depth = blocks.shape[1]

        # minimum permitted depth is 2
        if depth < 2:
            raise ErrorInitIndex('cannot create an IndexHierarchy from only one level.')
        if index_constructors is not None and len(index_constructors) != depth:
            raise ErrorInitIndex('if providing index constructors, number of index constructors must equal depth of IndexHierarchy.')

        depth_max = depth - 1
        depth_pre_max = depth - 2

        token = object()
        observed_last = [token for _ in range(depth)]
        range_depth = range(depth)

        tree = dict() # order assumed and necessary

        idx_row_last = -1
        for (idx_row, d), v in blocks.element_items():
            if idx_row_last != idx_row:
                # for each row, we re-set current to the outermost reference
                current = tree
                idx_row_last = idx_row

            if d < depth_pre_max:
                if v not in current:
                    current[v] = dict() # order necessary
                else:
                    # can only fetch this node (and not create a new node) if this is the sequential predecessor
                    if v != observed_last[d]:
                        raise ErrorInitIndex(f'invalid tree-form for IndexHierarchy: {v} cannot follow {observed_last[d]} when {v} has already been defined.')
                current = current[v]
                observed_last[d] = v
            elif d < depth_max: # premax means inner values are a list
                if v not in current:
                    current[v] = list()
                else:
                    # cannot just fetch this list if it is not the predecessor
                    if v != observed_last[d]:
                        raise ErrorInitIndex(f'invalid tree-form for IndexHierarchy: {v} cannot follow {observed_last[d]} when {v} has already been defined.')
                current = current[v]
                observed_last[d] = v
            elif d == depth_max: # at depth max
                # if there are redundancies here they will be caught in index creation
                current.append(v)
            else:
                raise ErrorInitIndex('label exceeded expected depth', v)

        # TODO: should find a way to explicitly pass dtypes per depth
        levels = cls._tree_to_index_level(
                tree,
                index_constructors=index_constructors
                )
        return cls(levels=levels, name=name, blocks=blocks, own_blocks=own_blocks)


    #---------------------------------------------------------------------------
    def __init__(self,
            levels: tp.Union[IndexLevel, 'IndexHierarchy'],
            *,
            name: NameType = None,
            blocks: tp.Optional[TypeBlocks] = None,
            own_blocks: bool = False,
            ):
        '''
        Args:
            levels: IndexLevels instance, or, optionally, an IndexHierarchy to be used to construct a new IndexHierarchy.
            labels: a client can optionally provide the labels used to construct the levels, as an optional optimization in forming the IndexHierarchy.
        '''

        if isinstance(levels, IndexHierarchy):
            if not blocks is None:
                raise ErrorInitIndex('cannot provide blocks when initializing with IndexHierarchy')
            # handle construction from another IndexHierarchy
            if levels._recache:
                levels._update_array_cache()

            # must deepcopy labels if not static;
            if self.STATIC and levels.STATIC:
                self._levels = levels._levels
            else:
                # passing level constructor ensures we get a mutable if the parent is mutable
                self._levels = levels._levels.to_index_level(
                        cls=self._LEVEL_CONSTRUCTOR
                        )
            # as the TypeBlocks managed by IndexHierarchy is never mutated in place, we could potentially share a reference here; perhaps a reason for distinct TypeBlocksGO
            self._blocks = levels._blocks.copy() # cache is up to date

            # transfer name if not given as arg
            if name is None and levels.name is not None:
                name = levels.name

        elif isinstance(levels, IndexLevel):
            # NOTE: perhaps better to use an own_levels parameter
            # always assume ownership of passed in IndexLevel
            self._levels = levels
            if blocks is not None:
                self._blocks = blocks if own_blocks else blocks.copy()
            else:
                self._blocks = None
        else:
            raise NotImplementedError(f'no handling for creation from {levels}')

        if self._blocks is not None:
            # self._length, self._depth = self._blocks.shape
            self._recache = False
        else:
            # self._depth = None
            # self._length = None
            self._recache = True
        self._name = name if name is None else name_filter(name)


    #---------------------------------------------------------------------------
    # name interface

    def rename(self: IH, name: tp.Hashable) -> IH:
        '''
        Return a new Frame with an updated name attribute.
        '''
        if self._recache:
            self._update_array_cache()
        # let the constructor handle reuse
        return self.__class__(self, name=name)

    #---------------------------------------------------------------------------
    # interfaces

    @property
    def loc(self) -> InterfaceGetItem:
        return InterfaceGetItem(self._extract_loc)

    @property
    def iloc(self) -> InterfaceGetItem:
        return InterfaceGetItem(self._extract_iloc)


    def _iter_label(self, depth_level: int = 0):
        yield from self._levels.label_nodes_at_depth(depth_level=depth_level)

    def _iter_label_items(self, depth_level: int = 0):
        yield from enumerate(self._levels.label_nodes_at_depth(depth_level=depth_level))

    @property
    def iter_label(self) -> IterNodeDepthLevel:
        return IterNodeDepthLevel(
                container=self,
                function_items=self._iter_label_items,
                function_values=self._iter_label,
                yield_type=IterNodeType.VALUES,
                apply_type=IterNodeApplyType.INDEX_LABELS
                )

    # NOTE: Index implements drop property

    @property
    @doc_inject(select='astype')
    def astype(self) -> InterfaceAsType:
        '''
        Retype one or more depths. Can be used as as function to retype the entire ``IndexHierarchy``; alternatively, a ``__getitem__`` interface permits retyping selected depths.

        Args:
            {dtype}
        '''
        return InterfaceAsType(func_getitem=self._extract_getitem_astype)

    #---------------------------------------------------------------------------

    def _update_array_cache(self):
        self._blocks = self._levels.to_type_blocks()
        # self._length, self._depth = self._blocks.shape
        self._recache = False

    #---------------------------------------------------------------------------

    @property # type: ignore
    @doc_inject()
    def mloc(self) -> int:
        '''{doc_int}
        '''
        if self._recache:
            self._update_array_cache()

        return self._blocks.mloc

    # @property
    # def dtypes(self) -> np.ndarray:
    #     '''
    #     Return the dtypes of the underlying NumPy array.

    #     Returns:
    #         np.ndarray
    #     '''
    #     if self._recache:
    #         self._update_array_cache()
    #     return self._blocks.dtypes

    @property
    def dtypes(self) -> 'Series':
        '''
        Return a Series of dytpes for each index depth.

        Returns:
            :obj:`static_frame.Series`
        '''
        from static_frame.core.series import Series

        if self._recache:
            self._update_array_cache()

        if self._name and len(self._name) == self.depth:
            labels = self._name
        else:
            labels = None

        return Series(self._blocks.dtypes, index=labels)


    @property
    def shape(self) -> tp.Tuple[int, ...]:
        '''
        Return a tuple describing the shape of the underlying NumPy array.

        Returns:
            :obj:`tp.Tuple[int]`
        '''
        if self._recache:
            self._update_array_cache()
        return self._blocks._shape

    @property
    def ndim(self) -> int:
        '''
        Return the number of dimensions.

        Returns:
            :obj:`int`
        '''
        return self._NDIM

    @property
    def size(self) -> int:
        '''
        Return the size of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()
        return self._blocks.size

    @property
    def nbytes(self) -> int:
        '''
        Return the total bytes of the underlying NumPy array.

        Returns:
            :obj:`int`
        '''
        if self._recache:
            self._update_array_cache()
        return self._blocks.nbytes

    def __bool__(self) -> bool:
        '''
        True if this container has size.
        '''
        if self._recache:
            self._update_array_cache()
        return bool(self._blocks.size)

    #---------------------------------------------------------------------------

    def __len__(self) -> int:
        if self._recache:
            self._update_array_cache()
        return self._blocks.__len__()

    @doc_inject()
    def display(self,
            config: tp.Optional[DisplayConfig] = None
            ) -> Display:
        '''{doc}

        Args:
            {config}
        '''
        config = config or DisplayActive.get()

        if self._recache:
            self._update_array_cache()

        sub_config = config
        sub_display = None

        for col in self._blocks.axis_values(0):
            # as a slice this is far more efficient as no copy is made
            if sub_display is None: # the first
                sub_display = Display.from_values(
                        col,
                        header=DisplayHeader(self.__class__, self._name),
                        config=sub_config,
                        outermost=True,
                        index_depth=0,
                        header_depth=1)
            else:
                sub_display.extend_iterable(col, header='')

        return sub_display


    #---------------------------------------------------------------------------
    def _drop_iloc(self, key: GetItemKeyType) -> 'IndexBase':
        '''Create a new index after removing the values specified by the loc key.
        '''
        if self._recache:
            self._update_array_cache()

        blocks = TypeBlocks.from_blocks(self._blocks._drop_blocks(row_key=key))
        index_constructors = tuple(self._levels.index_types())

        return self.__class__._from_type_blocks(blocks,
                index_constructors=index_constructors,
                name=self._name,
                own_blocks=True
                )

        # values = self._blocks.values

        # if key is None:
        #     if self.STATIC: # immutable, no selection, can return self
        #         return self
        #     labels = values # already immutable
        # elif isinstance(key, np.ndarray) and key.dtype == bool:
        #     # can use labels, as we already recached
        #     # use Boolean area to select indices from positions, as np.delete does not work with arrays
        #     labels = np.delete(values, self._positions[key], axis=0)
        #     labels.flags.writeable = False
        # else:
        #     labels = np.delete(values, key, axis=0)
        #     labels.flags.writeable = False

        # # from labels will work with both Index and IndexHierarchy
        # return self.__class__.from_labels(labels, name=self._name)

    def _drop_loc(self, key: GetItemKeyType) -> 'IndexBase':
        '''Create a new index after removing the values specified by the loc key.
        '''
        return self._drop_iloc(self.loc_to_iloc(key)) #type: ignore


    #---------------------------------------------------------------------------

    @property
    @doc_inject(selector='values_2d', class_name='IndexHierarchy')
    def values(self) -> np.ndarray:
        '''
        {}
        '''
        if self._recache:
            self._update_array_cache()
        return self._blocks.values

    @property
    def depth(self) -> int:
        if self._recache:
            self._update_array_cache()
        return self._blocks.shape[1]

    def values_at_depth(self,
            depth_level: DepthLevelSpecifier = 0
            ) -> np.ndarray:
        '''
        Return an NP array for the ``depth_level`` specified.

        Args:
            depth_level: a single depth level, or iterable depth of depth levels.
        '''
        if self._recache:
            self._update_array_cache()

        if isinstance(depth_level, int):
            sel = depth_level
        else:
            sel = list(depth_level)
        return self._blocks._extract_array(column_key=sel)


    @doc_inject()
    def label_widths_at_depth(self,
            depth_level: DepthLevelSpecifier = 0
            ) -> tp.Iterator[tp.Tuple[tp.Hashable, int]]:
        '''{}'''
        if isinstance(depth_level, int):
            sel = depth_level
        else:
            raise NotImplementedError('selection from iterables is not implemented')
        yield from self._levels.label_widths_at_depth(depth_level=depth_level)


    @property
    def index_types(self) -> 'Series':
        '''
        Return a Series of Index classes for each index depth.

        Returns:
            :obj:`static_frame.Series`
        '''
        from static_frame.core.series import Series

        if self._name and len(self._name) == self.depth:
            labels = self._name
        else:
            labels = None

        # NOTE: consider caching index_types
        return Series(self._levels.index_types(), index=labels)

    #---------------------------------------------------------------------------

    def copy(self: IH) -> IH:
        '''
        Return a new IndexHierarchy. This is not a deep copy.
        '''
        if self._recache:
            self._update_array_cache()

        blocks = self._blocks.copy()
        return self.__class__(
                levels=self._levels,
                name=self._name,
                blocks=blocks,
                own_blocks=True
                )


    def relabel(self, mapper: CallableOrMapping) -> 'IndexHierarchy':
        '''
        Return a new IndexHierarchy with labels replaced by the callable or mapping; order will be retained. If a mapping is used, the mapping should map tuple representation of labels, and need not map all origin keys.
        '''
        if self._recache:
            self._update_array_cache()

        index_constructors = tuple(self._levels.index_types())

        if not callable(mapper):
            # if a mapper, it must support both __getitem__ and __contains__
            getitem = getattr(mapper, 'get')

            def gen() -> tp.Iterator[tp.Tuple[tp.Hashable, ...]]:
                for array in self._blocks.axis_values(axis=1):
                    # as np.ndarray are not hashable, must tuplize
                    label = tuple(array)
                    yield getitem(label, label)

            return self.__class__.from_labels(gen(),
                    name=self._name,
                    index_constructors=index_constructors,
                    )

        return self.__class__.from_labels(
                (mapper(x) for x in self._blocks.axis_values(axis=1)),
                name=self._name,
                index_constructors=index_constructors,
                )

        # values = self._blocks.values

        # if not callable(mapper):
        #     # if a mapper, it must support both __getitem__ and __contains__; as np.ndarray are not hashable,
        #     # TODO: refactor with TB
        #     getitem = getattr(mapper, '__getitem__')

        #     labels = (tuple(x) for x in values)
        #     return self.__class__.from_labels(
        #             (getitem(x) if x in mapper else x for x in labels),
        #             name=self._name
        #             )

        # return self.__class__.from_labels(
        #         (mapper(x) for x in values),
        #         name=self._name
        #         )

    def rehierarch(self,
            depth_map: tp.Iterable[int]
            ) -> 'IndexHierarchy':
        '''
        Return a new `IndexHierarchy` that conforms to the new depth assignments given be `depth_map`.
        '''
        # TODO: refactor with TypeBlocks
        index, _ = rehierarch_and_map(
                labels=self.values,
                index_constructor=self.__class__.from_labels,
                depth_map=depth_map,
                )
        return index

    #---------------------------------------------------------------------------

    def loc_to_iloc(self,
            key: tp.Union[GetItemKeyType, HLoc]
            ) -> GetItemKeyType:
        '''
        Given iterable of GetItemKeyTypes, apply to each level of levels.
        '''
        from static_frame.core.series import Series

        # NOTE: this implementation is different from Index.loc_to_iloc: here, we explicitly translate Series, Index, and IndexHierarchy before passing on to IndexLevels

        if isinstance(key, Index):
            # if an Index, we simply use the values of the index
            key = key.values

        if isinstance(key, IndexHierarchy):
            # default iteration of IH is as tuple
            return [self._levels.leaf_loc_to_iloc(k) for k in key]

        if isinstance(key, Series):
            if key.dtype == bool:
                # if a Boolean series, sort and reindex
                if _requires_reindex(key.index, self):
                    key = key.reindex(self, fill_value=False).values
                else: # the index is equal
                    key = key.values
            else:
                # For all other Series types, we simply assume that the values are to be used as keys in the IH. This ignores the index, but it does not seem useful to require the Series, used like this, to have a matching index value, as the index and values would need to be identical to have the desired selection.
                key = key.values

        # if an HLoc, will pass on to loc_to_iloc
        return self._levels.loc_to_iloc(key)

    def _extract_iloc(self, key) -> tp.Union['IndexHierarchy', tp.Tuple[tp.Hashable]]:
        '''Extract a new index given an iloc key
        '''
        if self._recache:
            self._update_array_cache()

        if isinstance(key, INT_TYPES):
            # return a tuple if selecting a single row
            return tuple(self._blocks._extract_array(row_key=key))

        index_constructors = tuple(self._levels.index_types())
        tb = self._blocks._extract(row_key=key)

        return self.__class__._from_type_blocks(tb,
                name=self._name,
                index_constructors=index_constructors
                )

        # values = self._blocks.values
        # if key is None:
        #     labels = values
        # elif isinstance(key, slice):
        #     if key == NULL_SLICE:
        #         labels = values
        #     else:
        #         labels = values[key]
        # elif isinstance(key, KEY_ITERABLE_TYPES):
        #     # we assume Booleans have been normalized to integers here
        #     # can select directly from _blocks[key] if if key is a list
        #     labels = values[key]
        # else: # select a single label value: NOTE: convert array to tuple
        #     values = values[key]
        #     if values.ndim == 1:
        #         return tuple(values)
        #     raise NotImplementedError(
        #             'unhandled key type extracted a 2D array from labels') #pragma: no cover
        # return self.__class__.from_labels(labels=labels, name=self._name)

    def _extract_loc(self,
            key: GetItemKeyType
            ) -> tp.Union['IndexHierarchy', tp.Tuple[tp.Hashable]]:
        return self._extract_iloc(self.loc_to_iloc(key))

    def __getitem__(self, #pylint: disable=E0102
            key: GetItemKeyType
            ) -> tp.Union['IndexHierarchy', tp.Tuple[tp.Hashable]]:
        '''Extract a new index given an iloc key.
        '''
        return self._extract_iloc(key)


    #---------------------------------------------------------------------------

    def _extract_getitem_astype(self, key: GetItemKeyType) -> 'IndexHierarchyAsType':
        '''Given an iloc key (using integer positions for columns) return a configured IndexHierarchyAsType instance.
        '''
        # key is an iloc key
        if isinstance(key, tuple):
            raise KeyError('__getitem__ does not support multiple indexers')
        return IndexHierarchyAsType(self, key=key)



    #---------------------------------------------------------------------------
    # operators

    def _ufunc_unary_operator(self, operator: tp.Callable) -> np.ndarray:
        '''Always return an NP array.
        '''
        if self._recache:
            self._update_array_cache()

        values = self._blocks.values
        array = operator(values)
        array.flags.writeable = False
        return array

    def _ufunc_binary_operator(self, *, operator: tp.Callable, other) -> np.ndarray:
        '''
        Binary operators applied to an index always return an NP array. This deviates from Pandas, where some operations (multipling an int index by an int) result in a new Index, while other operations result in a np.array (using == on two Index).
        '''
        if self._recache:
            self._update_array_cache()
        values = self._blocks.values

        if isinstance(other, Index):
            # if this is a 1D index, must rotate labels before using an operator
            other = other.values.reshape((len(other), 1)) # operate on labels to labels
        elif isinstance(other, IndexHierarchy):
            # already 2D
            other = other.values # operate on labels to labels

        if operator.__name__ == 'matmul':
            return matmul(values, other)
        elif operator.__name__ == 'rmatmul':
            return matmul(other, values)

        array = operator(values, other)
        array.flags.writeable = False
        return array


    def _ufunc_axis_skipna(self, *,
            axis,
            skipna,
            ufunc,
            ufunc_skipna,
            composable: bool,
            dtypes: tp.Tuple[np.dtype, ...],
            size_one_unity: bool
            ) -> np.ndarray:
        '''
        Returns:
            immutable NumPy array.
        '''
        if self._recache:
            self._update_array_cache()

        dtype = None if not dtypes else dtypes[0]
        values = self._blocks.values

        if skipna:
            post = ufunc_skipna(values, axis=axis, dtype=dtype)
        else:
            post = ufunc(values, axis=axis, dtype=dtype)

        post.flags.writeable = False
        return post


    # _ufunc_shape_skipna defined in IndexBase

    #---------------------------------------------------------------------------
    # dictionary-like interface

    # NOTE: we intentionally exclude keys(), items(), and get() from Index classes, as they return inconsistent result when thought of as a dictionary


    def __iter__(self) -> tp.Iterator[tp.Tuple[tp.Hashable, ...]]:
        '''Iterate over labels.
        '''
        if self._recache:
            self._update_array_cache()

        for axis_values in self._blocks.axis_values(1):
            yield tuple(axis_values)

        # values = self._blocks.values
        # return tp.cast(tp.Iterator[tp.Hashable], array2d_to_tuples(values.__iter__()))

    def __reversed__(self) -> tp.Iterator[tp.Tuple[tp.Hashable, ...]]:
        '''
        Returns a reverse iterator on the index labels.
        '''
        if self._recache:
            self._update_array_cache()

        for axis_values in self._blocks.axis_values(1, reverse=True):
            yield tuple(axis_values)

        # values = self._blocks.values
        # return array2d_to_tuples(reversed(values))


    def __contains__(self, value) -> bool:
        '''Determine if a leaf loc is contained in this Index.
        '''
        # levels only, no need to recache as this is what has been mutated
        return self._levels.__contains__(value)



    #---------------------------------------------------------------------------
    # utility functions

    def sort(self,
            ascending: bool = True,
            kind: str = DEFAULT_SORT_KIND) -> 'Index':
        '''Return a new Index with the labels sorted.

        Args:
            kind: Sort algorithm passed to NumPy.
        '''
        if self._recache:
            self._update_array_cache()

        v = self._blocks.values
        order = np.lexsort([v[:, i] for i in range(v.shape[1]-1, -1, -1)])

        if not ascending:
            order = order[::-1]

        blocks = self._blocks._extract(row_key=order)
        index_constructors = tuple(self._levels.index_types())

        return self.__class__._from_type_blocks(blocks,
                index_constructors=index_constructors,
                name=self._name,
                own_blocks=True
                )

        # values = v[order]
        # values.flags.writeable = False
        # return self.__class__.from_labels(values, name=self._name)


    def isin(self, other: tp.Iterable[tp.Iterable[tp.Hashable]]) -> np.ndarray:
        '''
        Return a Boolean array showing True where one or more of the passed in iterable of labels is found in the index.
        '''
        if self._recache:
            self._update_array_cache()

        matches = []
        for seq in other:
            if not hasattr(seq, '__iter__'):
                raise RuntimeError('must provide one or more iterables within an iterable')
            # Coerce to hashable type
            as_tuple = tuple(seq)
            if len(as_tuple) == self.depth:
                # can pre-filter if iterable matches to length
                matches.append(as_tuple)

        if not matches:
            return np.full(self.__len__(), False, dtype=bool)

        return isin(self.flat().values, matches)


    def roll(self, shift: int) -> 'IndexHierarchy':
        '''Return an Index with values rotated forward and wrapped around (with a postive shift) or backward and wrapped around (with a negative shift).
        '''
        if self._recache:
            self._update_array_cache()

        blocks = TypeBlocks.from_blocks(
                self._blocks._shift_blocks(row_shift=shift, wrap=True)
                )
        index_constructors = tuple(self._levels.index_types())

        return self.__class__._from_type_blocks(blocks,
                index_constructors=index_constructors,
                name=self._name,
                own_blocks=True
                )


        # values = self._blocks.values

        # if shift % len(values):
        #     values = array_shift(
        #             array=values,
        #             shift=shift,
        #             axis=0,
        #             wrap=True)
        #     values.flags.writeable = False

        # # TODO: propagate index_constructors
        # return self.__class__.from_labels(values, name=self._name)



    #---------------------------------------------------------------------------
    # export

    def _to_frame(self,
            constructor: tp.Type[ContainerOperand]
            ) -> 'Frame':

        if self._recache:
            self._update_array_cache()

        return constructor(
                self._blocks.copy(), # TypeBlocks
                columns=None,
                index=None,
                own_data=True
                )

    def to_frame(self) -> 'Frame':
        '''
        Return :obj:`Frame` version of this :obj:`IndexHiearchy`.
        '''
        from static_frame import Frame
        return self._to_frame(Frame)

    def to_frame_go(self) -> 'FrameGO':
        '''
        Return a :obj:`FrameGO` version of this :obj:`IndexHierarchy`.
        '''
        from static_frame import FrameGO
        return self._to_frame(FrameGO)

    def to_pandas(self) -> 'DataFrame':
        '''Return a Pandas MultiIndex.
        '''
        import pandas

        if self._recache:
            self._update_array_cache()

        # must copy to get a mutable array
        arrays = tuple(a.copy() for a in self._blocks.axis_values(axis=0))
        mi = pandas.MultiIndex.from_arrays(arrays)

        mi.name = self._name
        mi.names = self.names
        return mi

    def flat(self) -> IndexBase:
        '''Return a flat, one-dimensional index of tuples for each level.
        '''
        return self._INDEX_CONSTRUCTOR(self.__iter__())

    def add_level(self, level: tp.Hashable):
        '''Return an IndexHierarchy with a new root level added.
        '''
        if self.STATIC: # can reuse levels
            levels_src = self._levels
        else:
            levels_src = self._levels.to_index_level()

        levels = self._LEVEL_CONSTRUCTOR(
                index=self._INDEX_CONSTRUCTOR((level,)),
                targets=ArrayGO([levels_src], own_iterable=True),
                offset=0,
                own_index=True
                )

        # NOTE: can transfrom TypeBlocks appropriately and pass to constructor
        return self.__class__(levels, name=self._name)

    def drop_level(self, count: int = 1) -> tp.Union[Index, 'IndexHierarchy']:
        '''Return an IndexHierarchy with one or more leaf levels removed. This might change the size of the index if the resulting levels are not unique.
        '''
        # NOTE: can transfrom TypeBlocks appropriately and pass to constructor

        if count < 0:
            levels = self._levels.to_index_level()
            for _ in range(abs(count)):
                levels_stack = [levels]
                while levels_stack:
                    level = levels_stack.pop()
                    # check to see if children of this target are leaves
                    if level.targets[0].targets is None:
                        level.targets = None
                    else:
                        levels_stack.extend(level.targets)
                if levels.targets is None:
                    # if our root level has no targets, we are at the root
                    break
            if levels.targets is None:
                # fall back to 1D index
                return levels.index
            return self.__class__(levels, name=self._name)

        elif count > 0:
            level = self._levels.to_index_level()
            for _ in range(count):
                # NOTE: do not need this check as we look ahead, below
                # if level.targets is None:
                #     return level.index
                targets = []
                labels = []
                for target in level.targets:
                    labels.extend(target.index)
                    if target.targets is not None:
                        targets.extend(target.targets)
                index = level.index.__class__(labels)
                if not targets:
                    return index
                level = level.__class__(index=index, targets=targets)
            return self.__class__(level, name=self._name)
        else:
            raise NotImplementedError('no handling for a 0 count drop level.')



class IndexHierarchyGO(IndexHierarchy):

    '''
    A hierarchy of :obj:`static_frame.Index` objects that permits mutation only in the addition of new hierarchies or labels.
    '''

    STATIC = False

    _IMMUTABLE_CONSTRUCTOR = IndexHierarchy

    _LEVEL_CONSTRUCTOR = IndexLevelGO
    _INDEX_CONSTRUCTOR = IndexGO

    __slots__ = (
            '_levels', # IndexLevel
            '_blocks',
            '_keys',
            '_recache',
            '_name'
            )

    def append(self, value: tuple):
        '''
        Append a single label to this index.
        '''
        self._levels.append(value)
        self._recache = True

    def extend(self, other: IndexHierarchy):
        '''
        Extend this IndexHiearchy in-place
        '''
        self._levels.extend(other._levels)
        self._recache = True

    def copy(self: IH) -> IH:
        '''
        Return a new IndexHierarchy. This is not a deep copy.
        '''
        if self._recache:
            self._update_array_cache()

        blocks = self._blocks.copy()
        return self.__class__(
                levels=self._levels.to_index_level(),
                name=self._name,
                blocks=blocks,
                own_blocks=True,
                )


# update class attr on Index after class initialziation
IndexHierarchy._MUTABLE_CONSTRUCTOR = IndexHierarchyGO



class IndexHierarchyAsType:

    __slots__ = ('container', 'key',)

    def __init__(self,
            container: IndexHierarchy,
            key: GetItemKeyType
            ) -> None:
        self.container = container
        self.key = key

    def __call__(self, dtype) -> 'IndexHierarchy':

        from static_frame.core.index_datetime import _dtype_to_index_cls
        container = self.container

        if container._recache:
            container._update_array_cache()

        # use TypeBlocks in both situations to avoid double casting
        blocks = TypeBlocks.from_blocks(
                container._blocks._astype_blocks(column_key=self.key, dtype=dtype)
                )

        # avoid coercion of datetime64 arrays that were not targetted in the selection
        index_constructors = container.index_types.values.copy()

        dtype_post = blocks.dtypes[self.key] # can select element or array
        if isinstance(dtype_post, np.dtype):
            index_constructors[self.key] = _dtype_to_index_cls(
                    container.STATIC,
                    dtype_post)
        else: # assign iterable
            index_constructors[self.key] = [
                    _dtype_to_index_cls(container.STATIC, dt)
                    for dt in dtype_post]

        return container.__class__._from_type_blocks(
                blocks,
                index_constructors=index_constructors,
                own_blocks=True
                )



