# -*- coding: utf-8 -*-
'''
The SFM parser module. It provides the basic stylesheet guided sfm parser 
and default TextType parser.  This parser provides detailed error and 
diagnostic messages including accurate line and column information as well
as context information for structure errors. 
The default marker meta data makes this produce only top-level marker-text
pairs.
'''
__version__ = '20101011'
__date__    = '11 October 2010'
__author__  = 'Tim Eves <tim_eves@sil.org>'
__history__ = '''
	20081210 - djd - Seperated SFM definitions from the module
		to allow for parsing other kinds of SFM models
		Also changed the name to parse_sfm.py as the
		module is more generalized now
	20091026 - tse - renamed and refactored generatoion of markers
		dict to module import time as part of import into palaso 
		package.
	20101026 - tse - rewrote to enable the parser to use the stylesheets to 
		direct how to parse document structure and TextType to permit
		specific semantics on subsections.
	20101028 - tse - Handle endmarkers as a special case so the do no require
		a space to separate them from the following text.
	20101109 - tse - Fix separator space being included when it shouldn't and
		use the unique field types set object to improve performance.
'''
import collections, codecs, functools, operator, re, warnings, os, sys
from itertools import chain, groupby
from functools import partial

try:
    from itertools import imap, ifilter
except ImportError:
    imap = map
    ifilter = filter

try:
    unicode
except NameError:
    unicode = str

__all__ = ('usfm',                                           # Sub modules
           'position','element','text', 'level', 'parser',   # data types
           'sreduce','smap','sfilter','mpath',
           'text_properties','pprint', 'copy')               # functions


'''Immutable position data that attach to tokens'''
class position(collections.namedtuple('position', ['line', 'col'])):
    def __str__(self):
        return 'line {0.line},{0.col}'.format(self)


class element(list):
    """
    A sequence type that for holding the a marker and it's child nodes
    >>> element('marker')
    element(u'marker')
    
    >>> str(element('marker'))
    '\\\\marker'
    
    >>> str(element('marker', args=['1','2']))
    '\\\\marker 1 2'
    
    >>> e = element('marker', args=['1'],
    ...             content=[text('some text '),
    ...                      element('marker2', content=[text('more text\\n')]),
    ...                      element('blah',content=[text('\\n')]),
    ...                      element('blah',content=[text('\\n')]),
    ...                      element('yak', args=['yak'])])
    >>> len(e)
    5
    >>> e.name
    u'marker'
    >>> e.pos
    position(line=1, col=1)
    >>> e.meta
    {}
    >>> print str(e)
    \\marker 1 some text \\marker2 more text
    \\blah
    \\blah
    \\yak yak
    >>> element('marker') == element('marker')
    True
    >>> element('marker') == element('different')
    False
    """
    __slots__ = ('pos', 'name', 'args', 'parent', 'meta', 'annotations')
    
    def __init__(self, name, pos=position(1,1), args=[], parent=None, meta={}, content=[]):
        super(element,self).__init__(content)
        self.name = unicode(name) if name else None
        self.pos = pos
        self.args = args
        self.parent = parent
        self.meta = meta
        self.annotations = {}
    
    def __repr__(self):
        args = [repr(self.name)] \
            + (self.args and [u'args=' + repr(self.args)]) \
            + (self   and [u'content=' + super(element,self).__repr__()])
        return u'element({0!s})'.format(', '.join(args))
    
    
    def __eq__(self, rhs):
        assert isinstance(rhs, element), 'rhs must be an element type'
        return self.name == rhs.name \
           and self.args == rhs.args \
           and (not self.meta or not rhs.meta or self.meta == rhs.meta) \
           and (not self.annotations or not rhs.annotations 
                                     or self.meta == rhs.meta) \
           and super(element,self).__eq__(rhs)
    
    def __str__(self):
        marker = (u'\\' + ' '.join([self.name] + self.args)) if self.name else ''
        endmarker = self.meta.get('Endmarker',u'')
        body = ''.join(imap(unicode, self))
        sep = ''
        if len(self) > 0:
            if isinstance(self[0], element) \
               and self.meta.get('StyleType') == 'Paragraph':
                sep = os.linesep
            elif not body.startswith(('\r\n','\n')):
                sep = u' '
        elif self.meta.get('StyleType') == 'Character' :
            body = ' '
        if endmarker and 'implicit-closed' not in self.annotations:
            body += u'\\' + endmarker
        return sep.join([marker, body])


class text(unicode):
    '''
    >>> from pprint import pprint

    An extended unicode string type that tracks position and 
    parent/child relationship.
    
    >>> text('a test')
    text(u'a test')
    
    >>> text('prefix ',position(3,10)).pos, text('suffix',position(1,6)).pos
    (position(line=3, col=10), position(line=1, col=6))
    
    >>> t = text('prefix ',position(3,10)) + text('suffix',position(1,6))
    >>> t, t.pos
    (text(u'prefix suffix'), position(line=3, col=10))
    
    >>> t = text('a few short words')[12:]
    >>> t, t.pos
    (text(u'words'), position(line=1, col=13))
    
    >>> t = text('   yuk spaces   ').lstrip()
    >>> t, t.pos
    (text(u'yuk spaces   '), position(line=1, col=4))
    
    >>> t = text('   yuk spaces   ').rstrip()
    >>> t, t.pos
    (text(u'   yuk spaces'), position(line=1, col=1))
    
    >>> text('   yuk spaces   ').strip()
    text(u'yuk spaces')
    
    >>> pprint([(t,t.pos) for t in text('a few short words').split(' ')])
    [(text(u'a'), position(line=1, col=1)),
     (text(u'few'), position(line=1, col=3)),
     (text(u'short'), position(line=1, col=7)),
     (text(u'words'), position(line=1, col=13))]
    
    >>> list(map(str, text('a few short words').split(' ')))
    ['a', 'few', 'short', 'words']
    
    >>> t=text.concat([text(u'a ', pos=position(line=1, col=1)), 
    ...                text(u'few ', pos=position(line=1, col=3)), 
    ...                text(u'short ', pos=position(line=1, col=7)),
    ...                text(u'words', pos=position(line=1, col=13))])
    >>> t, t.pos
    (text(u'a few short words'), position(line=1, col=1))
    '''
    def __new__(cls, content, pos=position(1,1), parent=None):
        return super(text,cls).__new__(cls, content)
    
    def __init__(self, content, pos=position(1,1), parent=None):
        self.pos = pos
        self.parent = parent
    
    @staticmethod
    def concat(iterable):
        i = iter(iterable)
        h = next(i)
        return text(u''.join(chain([h],i)), h.pos, h.parent)
    
    def split(self, sep, maxsplit=-1):
        tail = self
        result = []
        while tail and maxsplit != 0:
            e = tail.find(sep)
            if e == -1:
                result.append(tail)
                tail=text(u'',position(self.pos.line, self.pos.col+len(tail)), self.parent)
                break
            result.append(tail[:e])
            tail = tail[e+len(sep):]
            maxsplit -= 1
        if tail: result.append(tail)
        return result
    
    def lstrip(self,*args,**kwds):
        l = len(self)
        s_ = super(text,self).lstrip(*args,**kwds)
        return text(s_, position(self.pos.line, self.pos.col + l-len(s_)), self.parent)
    
    def rstrip(self,*args,**kwds):
        s_ = super(text,self).rstrip(*args, **kwds)
        return text(s_, self.pos, self.parent)
    
    def strip(self,*args,**kwds):
        return self.lstrip(*args, **kwds).rstrip(*args, **kwds)
    
    def __repr__(self):
        return u'text({0!s})'.format(super(text,self).__repr__())
    
    def __add__(self, rhs):
        return text(super(text,self).__add__(rhs), self.pos,self.parent)
    
    def __getslice__(self, i, j): return self.__getitem__(slice(i, j))
    
    def __getitem__(self,i):
        return text(super(text,self).__getitem__(i), 
                    position(self.pos.line, self.pos.col \
                                            + (i.start or 0 if isinstance(i, slice) else i)),
                    self.parent)


class _put_back_iter(collections.Iterator):
    '''
    >>> i=_put_back_iter([1,2,3])
    >>> next(i)
    1
    >>> next(i)
    2
    >>> i.put_back(256)
    >>> next(i)
    256
    >>> i.peek()
    3
    >>> i.put_back(512)
    >>> i.peek()
    512
    >>> next(i); next(i)
    512
    3
    >>> next(i)
    Traceback (most recent call last):
    ...
    StopIteration
    '''
    def __init__(self, iterable):
        self.__itr = iter(iterable)
        self.__pbq = []

    def __next__(self):
        return self.next()

    def next(self):
        if self.__pbq:
            try:    return self.__pbq.pop()
            except: raise StopIteration
        return next(self.__itr)

    def put_back(self, value):
        self.__pbq.append(value)

    def peek(self):
        if not self.__pbq:
            self.__pbq.append(next(self.__itr))
        return self.__pbq[-1]


_default_meta = {'TextType':'default', 'OccursUnder':set([None]), 'Endmarker':None, 'StyleType':None}

class level:
    Note            = -1
    Marker          =  0
    Content         =  1
    Structure       =  2
    Unrecoverable   =  100

class parser(collections.Iterable):
    '''
    >>> from pprint import pprint
    >>> import warnings
    
    Test edge case text handling
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("ignore")
    ...     list(parser([])); list(parser([''])); list(parser('plain text'))
    []
    []
    [text(u'plain text')]
    
    Test mixed marker and text and cross line coalescing
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("ignore")
    ...     pprint(list(parser(r"""\\lonely
    ... \\sfm text
    ... bare text
    ... \\more-sfm more text
    ... over a line break\\marker""".splitlines(True))))
    [element(u'lonely', content=[text(u'\\n')]),
     element(u'sfm', content=[text(u'text\\nbare text\\n')]),
     element(u'more-sfm', content=[text(u'more text\\nover a line break')]),
     element(u'marker')]
    
    Backslash handling
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("ignore")
    ...     pprint(list(parser([r"\\marker text",
    ...                         r"\\escaped backslash\\\\character"])))
    [element(u'marker', content=[text(u'text')]),
     element(u'escaped', content=[text(u'backslash\\\\\\\\character')])]
    
    >>> doc=r"""
    ... \\id MAT EN
    ... \\ide UTF-8
    ... \\rem from MATTHEW
    ... \\h Mathew
    ... \\toc1 Mathew
    ... \\mt1 Mathew
    ... \\mt2 Gospel Of Matthew"""
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("ignore")
    ...     pprint(list(parser(doc.splitlines(True))))
    [text(u'\\n'),
     element(u'id', content=[text(u'MAT EN\\n')]),
     element(u'ide', content=[text(u'UTF-8\\n')]),
     element(u'rem', content=[text(u'from MATTHEW\\n')]),
     element(u'h', content=[text(u'Mathew\\n')]),
     element(u'toc1', content=[text(u'Mathew\\n')]),
     element(u'mt1', content=[text(u'Mathew\\n')]),
     element(u'mt2', content=[text(u'Gospel Of Matthew')])]

    >>> tss = parser.extend_stylesheet({},'id','ide','rem','h','toc1','mt1','mt2')
    >>> pprint(tss)
    {'h': {'Endmarker': None,
           'OccursUnder': set([None]),
           'StyleType': None,
           'TextType': 'default'},
     'id': {'Endmarker': None,
            'OccursUnder': set([None]),
            'StyleType': None,
            'TextType': 'default'},
     'ide': {'Endmarker': None,
             'OccursUnder': set([None]),
             'StyleType': None,
             'TextType': 'default'},
     'mt1': {'Endmarker': None,
             'OccursUnder': set([None]),
             'StyleType': None,
             'TextType': 'default'},
     'mt2': {'Endmarker': None,
             'OccursUnder': set([None]),
             'StyleType': None,
             'TextType': 'default'},
     'rem': {'Endmarker': None,
             'OccursUnder': set([None]),
             'StyleType': None,
             'TextType': 'default'},
     'toc1': {'Endmarker': None,
              'OccursUnder': set([None]),
              'StyleType': None,
              'TextType': 'default'}}

    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("error")
    ...     pprint(list(parser(doc.splitlines(True), tss)))
    [text(u'\\n'),
     element(u'id', content=[text(u'MAT EN\\n')]),
     element(u'ide', content=[text(u'UTF-8\\n')]),
     element(u'rem', content=[text(u'from MATTHEW\\n')]),
     element(u'h', content=[text(u'Mathew\\n')]),
     element(u'toc1', content=[text(u'Mathew\\n')]),
     element(u'mt1', content=[text(u'Mathew\\n')]),
     element(u'mt2', content=[text(u'Gospel Of Matthew')])]
    >>> tss['rem'] = tss['rem'].copy()
    >>> tss['rem']['OccursUnder'] = set(['ide'])
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("error")
    ...     pprint(list(parser(doc.splitlines(True), tss)))
    [text(u'\\n'),
     element(u'id', content=[text(u'MAT EN\\n')]),
     element(u'ide', content=[text(u'UTF-8\\n'), element(u'rem', content=[text(u'from MATTHEW\\n')])]),
     element(u'h', content=[text(u'Mathew\\n')]),
     element(u'toc1', content=[text(u'Mathew\\n')]),
     element(u'mt1', content=[text(u'Mathew\\n')]),
     element(u'mt2', content=[text(u'Gospel Of Matthew')])]
    >>> del tss['mt1']
    >>> with warnings.catch_warnings():
    ...     warnings.simplefilter("error")
    ...     pprint(list(parser(doc.splitlines(True), tss)))
    Traceback (most recent call last):
    ...
    SyntaxWarning: <string>: line 7,2: unknown marker \mt1: not in styesheet
    '''
    
    default_meta = _default_meta
    _eos = text("end-of-file")
    __tokeniser = re.compile(r'(?<!\\)\\[^\s\\]+|(?:\\\\|[^\\])+',re.DOTALL | re.UNICODE)
    
    @classmethod
    def extend_stylesheet(cls, stylesheet, *names):
        return dict(((m,cls.default_meta) for m in names), **stylesheet)
    
    
    def __init__(self, source, stylesheet={}, 
                               default_meta=_default_meta, 
                               private_prefix=None, error_level=level.Content):
        # Pick the marker lookup failure mode.
        assert default_meta or not private_prefix, 'default_meta must be provided when using private_prefix'
        assert error_level <= level.Marker or default_meta, 'default meta must be provided when error_level > level.Marker'
        
        # Set simple attributes
        self.source = getattr(source, 'name', '<string>')
        self.__default_meta = default_meta
        self.__pua_prefix   = private_prefix
        self._tokens        = _put_back_iter(self.__lexer(source))
        self._error_level   = error_level
        
        # Compute end marker stylesheet definitions
        em_def = {'TextType':None, 'Endmarker':None}
        self.__sty = stylesheet.copy()
        self.__sty.update((m['Endmarker'], dict(em_def, OccursUnder=set([k]))) 
                               for k, m in stylesheet.items() if m['Endmarker'])
    
    
    def _error(self, severity, msg, ev, *args, **kwds):
        if severity >= 0  and severity >= self._error_level:
            msg = (u'{source}: line {token.pos.line},{token.pos.col}: ' + unicode(msg)).format(token=ev,source=self.source,
                                                                                               *args,**kwds)
            raise SyntaxError(msg)
        elif severity < 0 and severity < self._error_level: 
            pass
        else:
            msg = (u'{source}: line {token.pos.line},{token.pos.col}: ' + unicode(msg)).format(token=ev,source=self.source,
                                                                                               *args,**kwds)
            warnings.warn_explicit(msg, SyntaxWarning, self.source, ev.pos.line)
    
    
    def __get_style(self, tag):
        meta = self.__sty.get(tag)
        if not meta:
            if self.__pua_prefix and tag.startswith(self.__pua_prefix):
                self._error(level.Note, 
                            'unknown private marker \\{token}: '
                            'not it stylesheet using default marker definition', 
                            tag)
            else:
                self._error(level.Marker, 'unknown marker \\{token}: not in styesheet', 
                            tag)
            return self.__default_meta

        return meta
    
    
    def __iter__(self):
        return self._default_(None)
    
    
    @staticmethod
    def _pp_marker_list(tags):
        return ', '.join('\\'+c if c else 'toplevel' for c in sorted(tags))
    
    
    @staticmethod
    def __lexer(lines):
        """ Return an iterator that returns tokens in a sequence:
            marker, text, marker, text, ...
        """
        lmss = enumerate(imap(parser.__tokeniser.finditer, lines))
        fs = (text(m.group(), position(l+1,m.start()+1)) for l,ms in lmss for m in ms)
        gs = groupby(fs, operator.methodcaller('startswith','\\'))
        return chain.from_iterable(g if istag else (text.concat(g),) for istag,g in gs)
    
    
    def __extract_tag(self, parent, tok):
        # Check for the expected end markers with no separator and
        # break them apart
        if parent is not None:
            tag = tok[1:]
            while parent.meta['Endmarker']:
                if tag.startswith(parent.meta['Endmarker']):
                    cut = len(parent.meta['Endmarker'])
                    if cut != len(tag):
                        if self._tokens.peek()[0] == u'\\':
                            self._tokens.put_back(tag[cut:])
                        else:
                            # If the next token isn't a marker coaleces the 
                            # remainder with it into a single text node.
                            self._tokens.put_back(tag[cut:] + next(self._tokens,''))
                        return tok[:cut+1]
                    # TODO: what happens if cut == len(tag), why doesn't it return here?
                parent = parent.parent
        return tok
    
    
    def _default_(self, parent):
        get_meta = self.__get_style
        for tok in self._tokens:
            if tok[0] == u'\\':  # Parse markers.
                tok  = self.__extract_tag(parent, tok)
                tag = tok[1:]
                meta = get_meta(tag)
                if not meta['OccursUnder'] or (parent is not None and parent.name or None) in meta['OccursUnder']:
                    sub_parse = meta['TextType']
                    if not sub_parse: return
                    
                    # Spawn a sub-node
                    e = element(tag, tok.pos, parent=parent, meta=meta)
                    # and recurse
                    e.extend(getattr(self,'_'+sub_parse+'_',self._default_) (e))
                    yield e
                elif parent is None:
                    # We've failed to find a home for marker tag, poor thing.
                    if not meta['TextType']:
                        assert len(meta['OccursUnder']) == 1
                        self._error(level.Unrecoverable, 
                                      'orphan end marker {token}: '
                                      'no matching opening marker \\{0}', 
                                      tok, list(meta['OccursUnder'])[0])
                    else:
                        self._error(level.Unrecoverable, 
                                      'orphan marker {token}: '
                                      'may only occur under {0}', tok, 
                                      self._pp_marker_list(meta['OccursUnder']))
                else:
                    # Do implicit closure only for non-inline markers or 
                    # markers inside NoteText type markers'.
                    if parent.meta['Endmarker']:
                        self._force_close(parent, tok)
                        parent.annotations['implicit-closed'] = True
                    self._tokens.put_back(tok)
                    return
            else:   # Pass non marker data through with a litte fix-up
                if parent is not None \
                        and len(parent) == 0 \
                        and not tok.startswith(('\r\n','\n')):
                    tok = tok[1:]
                if tok:
                    tok.parent = parent
                    yield tok
        if parent is not None:
            if parent.meta['Endmarker']:
                self._force_close(parent, self._eos)
                parent.annotations['implicit-closed'] = True

    def _Milestone_(self, parent):
        return tuple()
    _milestone_ = _Milestone_
    
    _Other_ = _default_
    _other_ = _Other_

    def _force_close(self, parent, tok):
        self._error(level.Structure, 
            'invalid end marker {token}: \\{0.name} '
            '(line {0.pos.line},{0.pos.col}) '
            'can only be closed with \\{1}', tok, parent,
            parent.meta['Endmarker'])
         

def sreduce(elementf, textf, trees, initial=None):
    def _g(a, e):
        if isinstance(e, basestring): return textf(e,a)
        return elementf(e, a, reduce(_g, e, initial))
    return reduce(_g, trees, initial)



def smap(elementf, textf, doc):
    def _g(e):
        if isinstance(e,element):
            (name,args,cs) = elementf(e.name, e.args, map(_g, e))
            e = element(name, e.pos, args, content=cs, meta=e.meta)
            reduce(lambda _,e_: setattr(e_,'parent',e), e, None)
            return e
        else:
            e_ = textf(e)
            return text(e_, e.pos, e)
    return map(_g, doc)



def sfilter(pred, doc):
    def _g(a, e):
        if isinstance(e, text): 
            if pred(e.parent):
                a.append(text(e,e.pos,a or None))
            return a
        e_ = element(e.name, e.pos, e.args, parent=a or None, meta=e.meta)
        reduce(_g, e, e_)
        if len(e_) or pred(e):
            a.append(e_)
        return a
    return reduce(_g, doc, [])



def _path(e):
    r = []
    while (e != None): 
        r.append(e.name)
        e = e.parent
    r.reverse()
    return r



def mpath(*path):
    path = list(path)
    pr_slice = slice(len(path))
    def _pred(e): return path == _path(e)[pr_slice]
    return _pred



def text_properties(*props):
    props = set(props)
    def _props(e): return props <= set(e.meta.get('TextProperties',[]))
    return _props



def pprint(doc):
    return ''.join(imap(unicode, doc))


def copy(doc):
    def id_element(name, args, children): return (name, args[:], children)
    def id_text(t): return t
    return smap(id_element, id_text, doc)

