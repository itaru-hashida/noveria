# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""РРµСЂР°СЂС…РёС‡РµСЃРєРёРµ РјР°СЂС€СЂСѓС‚С‹ HPA*: РєР»РµС‚РєРё -> СЂРµРіРёРѕРЅС‹ -> entrances -> РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ.

Р—Р°С‡РµРј РѕС‚РґРµР»СЊРЅРѕРµ СЏРґСЂРѕ. ``route_kernel.single_source`` СЃС‚СЂРѕРёС‚ РґРµСЂРµРІРѕ РѕС‚ РёСЃС‚РѕРєР°
РЅР° РІСЃСЋ РєР°СЂС‚Сѓ, РїРѕСЌС‚РѕРјСѓ С†РµРЅР° РѕРґРЅРѕРіРѕ РїСЂРѕРјР°С…Р° РєСЌС€Р° СЂР°СЃС‚С‘С‚ Р»РёРЅРµР№РЅРѕ РѕС‚ СЂР°Р·РјРµСЂР° РєР°СЂС‚С‹.
Р”Р»СЏ Р±РѕР»СЊС€РёС… РєР°СЂС‚ РЅСѓР¶РµРЅ РїРѕРёСЃРє В«С‚РѕС‡РєР°-С‚РѕС‡РєР°В», С‡РёС‚Р°СЋС‰РёР№ РЅРµ РІСЃСЋ РєР°СЂС‚Сѓ, Р° РµС‘
СЂР°Р·Р±РёРµРЅРёРµ РЅР° СЂРµРіРёРѕРЅС‹.

Р”РІРµ СЃС‚СѓРїРµРЅРё РїРѕРёСЃРєР° (РѕР±Рµ РІ Cython; РІ СЂР°Р±РѕС‡РµРј РїРѕРёСЃРєРµ РЅРµС‚ Python-СЃР»РѕРІР°СЂРµР№ Рё
Python-РѕР±СЉРµРєС‚РѕРІ РІ СѓР·Р»Р°С…):

  1. РРµСЂР°СЂС…РёСЏ. РљР°СЂС‚Р° СЂРµР¶РµС‚СЃСЏ РЅР° РєРІР°РґСЂР°С‚РЅС‹Рµ СЂРµРіРёРѕРЅС‹ ``chunk x chunk`` (РїРѕ
     СѓРјРѕР»С‡Р°РЅРёСЋ 32). Р’С…РѕРґС‹ (entrances) вЂ” РєР»РµС‚РєРё СЃ СЃРѕСЃРµРґРѕРј РІ РґСЂСѓРіРѕРј СЂРµРіРёРѕРЅРµ.
     Р”Р»СЏ РєР°Р¶РґРѕРіРѕ СЂРµРіРёРѕРЅР° Р»РµРЅРёРІРѕ СЃС‡РёС‚Р°СЋС‚СЃСЏ С‚РѕС‡РЅС‹Рµ Р»РѕРєР°Р»СЊРЅС‹Рµ СЂР°СЃСЃС‚РѕСЏРЅРёСЏ РјРµР¶РґСѓ
     РµРіРѕ РІС…РѕРґР°РјРё (Dijkstra, РѕРіСЂР°РЅРёС‡РµРЅРЅС‹Р№ РєР»РµС‚РєР°РјРё СЂРµРіРёРѕРЅР°), РёР· РЅРёС… Рё РёР·
     РјРµР¶СЂРµРіРёРѕРЅР°Р»СЊРЅС‹С… СЂС‘Р±РµСЂ СЃС‚СЂРѕРёС‚СЃСЏ РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ. A* РїРѕ РіСЂР°С„Сѓ СЂРµРіРёРѕРЅРѕРІ СЃ
     СЌРІСЂРёСЃС‚РёРєРѕР№ ``min_w x РјР°РЅС…СЌС‚С‚РµРЅ`` РґР°С‘С‚ С†РµРїРѕС‡РєСѓ СЂРµРіРёРѕРЅРѕРІ Рё РїСѓС‚СЊ, Р° Р·РЅР°С‡РёС‚
     Рё РІРµСЂС…РЅСЋСЋ РіСЂР°РЅРёС†Сѓ СЃС‚РѕРёРјРѕСЃС‚Рё.
  2. РЈС‚РѕС‡РЅРµРЅРёРµ. РўРѕС‡РЅС‹Р№ A* РїРѕ РєР»РµС‚РєР°Рј СЃ РґРѕРїСѓСЃС‚РёРјРѕР№ Рё СЃРѕРіР»Р°СЃРѕРІР°РЅРЅРѕР№ СЌРІСЂРёСЃС‚РёРєРѕР№
     ``min_w x РјР°РЅС…СЌС‚С‚РµРЅ``, РѕР±СЂРµР·Р°РЅРЅС‹Р№ СЃРІРµСЂС…Сѓ РЅР°Р№РґРµРЅРЅРѕР№ РіСЂР°РЅРёС†РµР№. РћРЅ РґР°С‘С‚
     С‚РѕС‡РЅРѕРµ СЂР°СЃСЃС‚РѕСЏРЅРёРµ Рё СЂРѕРІРЅРѕ С‚РѕС‚ РєСЂР°С‚С‡Р°Р№С€РёР№ РїСѓС‚СЊ, РєРѕС‚РѕСЂС‹Р№ РІРµСЂРЅСѓР» Р±С‹ РїРѕР»РЅС‹Р№
     Dijkstra, РµСЃР»Рё СЌС‚РѕС‚ РїСѓС‚СЊ РµРґРёРЅСЃС‚РІРµРЅРµРЅ.

РџРѕС‡РµРјСѓ РіСЂР°РЅРёС†Р° СЃРІРµСЂС…Сѓ, Р° РЅРµ В«С‚РѕС‡РЅС‹Р№ HPA*В». Р›РѕРєР°Р»СЊРЅРѕРµ СЂР°СЃСЃС‚РѕСЏРЅРёРµ РјРµР¶РґСѓ РІС…РѕРґР°РјРё
СЃС‡РёС‚Р°РµС‚СЃСЏ РІРЅСѓС‚СЂРё СЂРµРіРёРѕРЅР°, РїРѕСЌС‚РѕРјСѓ Р»СЋР±РѕР№ РїСѓС‚СЊ РІ РіСЂР°С„Рµ СЂРµРіРёРѕРЅРѕРІ СЃРєР»РµРёРІР°РµС‚СЃСЏ РІ
СЂРµР°Р»СЊРЅС‹Р№ РїСѓС‚СЊ РєР°СЂС‚С‹: РµРіРѕ СЃС‚РѕРёРјРѕСЃС‚СЊ РЅРµ РјРµРЅСЊС€Рµ РёСЃС‚РёРЅРЅРѕР№. Р—РЅР°С‡РёС‚ РёРµСЂР°СЂС…РёСЏ РґР°С‘С‚
РѕС†РµРЅРєСѓ СЃРІРµСЂС…Сѓ (РѕР±С‹С‡РЅРѕ РѕС‡РµРЅСЊ С‚РµСЃРЅСѓСЋ), Р° С‚РѕС‡РЅРѕСЃС‚СЊ РѕР±РµСЃРїРµС‡РёРІР°РµС‚ РІС‚РѕСЂР°СЏ СЃС‚СѓРїРµРЅСЊ:
РµС‘ СЌРІСЂРёСЃС‚РёРєР° вЂ” РѕС†РµРЅРєР° СЃРЅРёР·Сѓ, РїРѕСЌС‚РѕРјСѓ A* С‚РѕС‡РµРЅ. РљР»Р°СЃСЃРёС‡РµСЃРєР°СЏ РѕС€РёР±РєР° В«HPA*
С‚РѕС‡РµРЅ СЃР°Рј РїРѕ СЃРµР±РµВ» РѕРїСЂРѕРІРµСЂРіР°РµС‚СЃСЏ РЅР° СЂРµР°Р»СЊРЅС‹С… РєР°СЂС‚Р°С…: РѕРїС‚РёРјР°Р»СЊРЅС‹Р№ РїСѓС‚СЊ РјРµР¶РґСѓ
РґРІСѓРјСЏ РІС…РѕРґР°РјРё РјРѕР¶РµС‚ РІС‹Р№С‚Рё Р·Р° РїСЂРµРґРµР»С‹ СЂРµРіРёРѕРЅР°, Рё Р»РѕРєР°Р»СЊРЅР°СЏ С‚Р°Р±Р»РёС†Р° Р·Р°РІС‹С€Р°РµС‚
СЃС‚РѕРёРјРѕСЃС‚СЊ. РџРѕСЌС‚РѕРјСѓ РёРµСЂР°СЂС…РёСЏ Р·РґРµСЃСЊ вЂ” СѓСЃРєРѕСЂРёС‚РµР»СЊ, Р° РЅРµ РёСЃС‚РѕС‡РЅРёРє РёСЃС‚РёРЅС‹.

Parity-РєРѕРЅС‚СЂР°РєС‚ СЃ Dijkstra. РЎРѕРІРїР°РґР°РµС‚ РЅРµ С‚РѕР»СЊРєРѕ СЃС‚РѕРёРјРѕСЃС‚СЊ, РЅРѕ Рё СЃР°Рј РїСѓС‚СЊ.
РљСЂР°С‚С‡Р°Р№С€РёР№ РїСѓС‚СЊ РѕРґРЅРѕР·РЅР°С‡РµРЅ С‚РѕРіРґР° Рё С‚РѕР»СЊРєРѕ С‚РѕРіРґР°, РєРѕРіРґР° Сѓ РєР°Р¶РґРѕР№ РІРµСЂС€РёРЅС‹ РЅР°
РЅС‘Рј СЂРѕРІРЅРѕ РѕРґРёРЅ РїСЂРµРґРѕРє СЃ С‚РѕР№ Р¶Рµ РґРёСЃС‚Р°РЅС†РёРµР№. РЇРґСЂРѕ СЃС‡РёС‚Р°РµС‚ С‡РёСЃР»Рѕ РѕРїС‚РёРјР°Р»СЊРЅС‹С…
РїСѓС‚РµР№ (РЅР°СЃС‹С‰Р°СЏ РЅР° РґРІСѓС…) РЅР° СѓР·Р»Р°С…, СѓР»РѕР¶РёРІС€РёС…СЃСЏ РІ РіСЂР°РЅРёС†Сѓ, Рё РїСЂРё С‡РёСЃР»Рµ Р±РѕР»СЊС€Рµ
РµРґРёРЅРёС†С‹ РІРѕР·РІСЂР°С‰Р°РµС‚ ``None``: РїСЂРµРґРєР° РїСЂРё СЂР°РІРЅС‹С… РїСѓС‚СЏС… РІС‹Р±РёСЂР°РµС‚ РїРѕСЂСЏРґРѕРє РєСѓС‡Рё
Dijkstra, Рё СѓРіР°РґС‹РІР°С‚СЊ РµРіРѕ РёРµСЂР°СЂС…РёСЏ РЅРµ РёРјРµРµС‚ РїСЂР°РІР°. РўРѕРіРґР° Python-РіСЂР°РЅРёС†Р° Р±РµСЂС‘С‚
РїСЂРµР¶РЅРёР№ single-source Dijkstra. РџСЂРё РµРґРёРЅСЃС‚РІРµРЅРЅРѕРј РєСЂР°С‚С‡Р°Р№С€РµРј РїСѓС‚Рё
РїРѕСЃР»РµРґРѕРІР°С‚РµР»СЊРЅРѕСЃС‚СЊ СѓР·Р»РѕРІ РѕРґРЅР° Рё С‚Р° Р¶Рµ Сѓ Р»СЋР±РѕРіРѕ РєРѕСЂСЂРµРєС‚РЅРѕРіРѕ Р°Р»РіРѕСЂРёС‚РјР°.

Р§РµРіРѕ СЏРґСЂРѕ РЅРµ РґРµР»Р°РµС‚: РЅРµ С…СЂР°РЅРёС‚ РіСЂР°С„Р°-РІР»Р°РґРµР»СЊС†Р° Рё РЅРµ Р·Р°РІРѕРґРёС‚ РєСЌС€Р° РјР°СЂС€СЂСѓС‚РѕРІ.
Р–РёРІРѕР№ ``graph._adj`` С‡РёС‚Р°РµС‚СЃСЏ С‚РѕР»СЊРєРѕ РїСЂРё СЃР±РѕСЂРєРµ Рё РѕР±РЅРѕРІР»РµРЅРёРё СЂРµРіРёРѕРЅРѕРІ; СЃР°Рј
РїРѕРёСЃРє РёРґС‘С‚ РїРѕ РєРѕРјРїР°РєС‚РЅС‹Рј РјР°СЃСЃРёРІР°Рј. Р РµС€РµРЅРёРµ В«Р·РІР°С‚СЊ РёРµСЂР°СЂС…РёСЋ РёР»Рё РїСЂРµР¶РЅРёР№
DijkstraВ» РїСЂРёРЅРёРјР°РµС‚ Python-РіСЂР°РЅРёС†Р°; РЅРµРїРѕРґРґРµСЂР¶РёРІР°РµРјС‹Р№ РІС…РѕРґ РѕРЅР° РѕС‚РІРµСЂРіР°РµС‚ РґРѕ
СЃР±РѕСЂРєРё (multigraph, oriented, РЅРµС‡РёСЃР»РѕРІС‹Рµ/РѕС‚СЂРёС†Р°С‚РµР»СЊРЅС‹Рµ РІРµСЃР°, СѓР·Р»С‹ Р±РµР·
РєРѕРѕСЂРґРёРЅР°С‚, СЂС‘Р±СЂР° РЅРµ РїРѕ СЃРµС‚РєРµ).

РРЅРІР°Р»РёРґР°С†РёСЏ РїРѕ СЂРµРіРёРѕРЅР°Рј: ``invalidate(nodes)`` РїРѕРјРµС‡Р°РµС‚ С‚РѕР»СЊРєРѕ СЂРµРіРёРѕРЅС‹ СЌС‚РёС…
РєР»РµС‚РѕРє. РџСЂРё СЃР»РµРґСѓСЋС‰РµРј РїРѕРёСЃРєРµ Р»РѕРєР°Р»СЊРЅС‹Рµ С‚Р°Р±Р»РёС†С‹ С‡РёСЃС‚С‹С… СЂРµРіРёРѕРЅРѕРІ СЃРѕС…СЂР°РЅСЏСЋС‚СЃСЏ,
Р° РіСЂСЏР·РЅС‹С… вЂ” РїРµСЂРµСЃРѕР±РёСЂР°СЋС‚СЃСЏ.
"""
from libc.math cimport INFINITY
from libc.stdlib cimport free, malloc, realloc
from libc.string cimport memset

cdef double _INF = INFINITY


cdef class RegionGraph:
    """Р Р°Р·Р±РёРµРЅРёРµ РєР°СЂС‚С‹ Рё РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ РЅР° РєРѕРјРїР°РєС‚РЅС‹С… РјР°СЃСЃРёРІР°С….

    РќРё РѕРґРёРЅ СѓР·РµР» СЂР°Р±РѕС‡РµРіРѕ РїРѕРёСЃРєР° РЅРµ СЏРІР»СЏРµС‚СЃСЏ Python-РѕР±СЉРµРєС‚РѕРј: РєРѕРѕСЂРґРёРЅР°С‚С‹,
    РїСЂРёРЅР°РґР»РµР¶РЅРѕСЃС‚СЊ СЂРµРіРёРѕРЅСѓ, CSR СЂС‘Р±РµСЂ, РІС…РѕРґС‹, Р»РѕРєР°Р»СЊРЅС‹Рµ С‚Р°Р±Р»РёС†С‹ Рё РєСѓС‡Р° Р¶РёРІСѓС‚
    РІ РјР°СЃСЃРёРІР°С… C, РІС‹РґРµР»РµРЅРЅС‹С… malloc. Python-СЃР»РѕРІР°СЂРё СѓС‡Р°СЃС‚РІСѓСЋС‚ С‚РѕР»СЊРєРѕ РІ СЃР±РѕСЂРєРµ
    (``index_of`` Рё Р¶РёРІРѕР№ ``adj``).
    """

    cdef int n
    cdef int chunk
    cdef int cols
    cdef int rows
    cdef int nreg
    cdef int ninst
    cdef double min_w
    cdef int grid_ok
    cdef object ids
    cdef object index_of
    cdef object adj_ref

    cdef int* xs
    cdef int* ys
    cdef int* region_of

    cdef int* off
    cdef int* adj
    cdef double* wt

    cdef int* roff
    cdef int* rnodes

    cdef int* ioff
    cdef int* inode
    cdef int* iof
    cdef int* ireg

    cdef double** row_tab
    cdef char* row_ready
    cdef char* dirty
    cdef int dirty_any

    # РЈС‚РѕС‡РЅСЏСЋС‰РёР№ A* РїРѕ РєР»РµС‚РєР°Рј: С‚Рµ Р¶Рµ РјР°СЃСЃРёРІС‹, С‡С‚Рѕ Сѓ Р»РѕРєР°Р»СЊРЅРѕРіРѕ Dijkstra,
    # РїР»СЋСЃ РїРѕСЂСЏРґРѕРє С„РёРєСЃР°С†РёРё Рё СЃС‡С‘С‚С‡РёРє РѕРїС‚РёРјР°Р»СЊРЅС‹С… РїСѓС‚РµР№.
    cdef double* ldist
    cdef int* lstamp
    cdef int* lpred
    cdef int* sorder
    cdef char* scount
    cdef int nsettled
    cdef int token

    # РРµСЂР°СЂС…РёС‡РµСЃРєРёР№ A* РїРѕ СЃР»РѕС‚Р°Рј РіСЂР°С„Р° СЂРµРіРёРѕРЅРѕРІ.
    cdef double* gdist
    cdef double* gpush
    cdef int* gpar
    cdef int* gstamp
    cdef char* gequal
    cdef int gtoken

    cdef double* heap_d
    cdef long* heap_c
    cdef int* heap_v
    cdef int heap_size
    cdef int heap_cap
    cdef long heap_tape

    cdef int start_index
    cdef int goal_index
    cdef int ambiguous
    cdef int segments

    def __cinit__(self):
        self.xs = NULL
        self.ys = NULL
        self.region_of = NULL
        self.off = NULL
        self.adj = NULL
        self.wt = NULL
        self.roff = NULL
        self.rnodes = NULL
        self.ioff = NULL
        self.inode = NULL
        self.iof = NULL
        self.ireg = NULL
        self.row_tab = NULL
        self.row_ready = NULL
        self.dirty = NULL
        self.ldist = NULL
        self.lstamp = NULL
        self.lpred = NULL
        self.sorder = NULL
        self.scount = NULL
        self.gdist = NULL
        self.gpush = NULL
        self.gpar = NULL
        self.gstamp = NULL
        self.gequal = NULL
        self.heap_d = NULL
        self.heap_c = NULL
        self.heap_v = NULL
        self.n = 0
        self.nreg = 0
        self.ninst = 0
        self.heap_size = 0
        self.heap_cap = 0
        self.dirty_any = 0
        self.ambiguous = 0
        self.grid_ok = 1

    def __dealloc__(self):
        self._free_all()

    cdef void _free_all(self):
        cdef int r
        if self.xs != NULL:
            free(self.xs)
            self.xs = NULL
        if self.ys != NULL:
            free(self.ys)
            self.ys = NULL
        if self.region_of != NULL:
            free(self.region_of)
            self.region_of = NULL
        if self.roff != NULL:
            free(self.roff)
            self.roff = NULL
        if self.rnodes != NULL:
            free(self.rnodes)
            self.rnodes = NULL
        if self.ioff != NULL:
            free(self.ioff)
            self.ioff = NULL
        if self.inode != NULL:
            free(self.inode)
            self.inode = NULL
        if self.iof != NULL:
            free(self.iof)
            self.iof = NULL
        if self.ireg != NULL:
            free(self.ireg)
            self.ireg = NULL
        if self.row_tab != NULL:
            for r in range(self.nreg):
                if self.row_tab[r] != NULL:
                    free(self.row_tab[r])
            free(self.row_tab)
            self.row_tab = NULL
        if self.row_ready != NULL:
            free(self.row_ready)
            self.row_ready = NULL
        if self.dirty != NULL:
            free(self.dirty)
            self.dirty = NULL
        self._free_scratch()
        self._free_csr()

    cdef void _free_csr(self):
        if self.off != NULL:
            free(self.off)
            self.off = NULL
        if self.adj != NULL:
            free(self.adj)
            self.adj = NULL
        if self.wt != NULL:
            free(self.wt)
            self.wt = NULL

    cdef void _free_scratch(self):
        if self.ldist != NULL:
            free(self.ldist)
            self.ldist = NULL
        if self.lstamp != NULL:
            free(self.lstamp)
            self.lstamp = NULL
        if self.lpred != NULL:
            free(self.lpred)
            self.lpred = NULL
        if self.sorder != NULL:
            free(self.sorder)
            self.sorder = NULL
        if self.scount != NULL:
            free(self.scount)
            self.scount = NULL
        if self.gdist != NULL:
            free(self.gdist)
            self.gdist = NULL
        if self.gpush != NULL:
            free(self.gpush)
            self.gpush = NULL
        if self.gpar != NULL:
            free(self.gpar)
            self.gpar = NULL
        if self.gstamp != NULL:
            free(self.gstamp)
            self.gstamp = NULL
        if self.gequal != NULL:
            free(self.gequal)
            self.gequal = NULL
        if self.heap_d != NULL:
            free(self.heap_d)
            self.heap_d = NULL
        if self.heap_c != NULL:
            free(self.heap_c)
            self.heap_c = NULL
        if self.heap_v != NULL:
            free(self.heap_v)
            self.heap_v = NULL
        self.heap_cap = 0
        self.heap_size = 0

    # ------------------------------------------------------------------
    # Р”РёР°РіРЅРѕСЃС‚РёРєР° РґР»СЏ Python-РіСЂР°РЅРёС†С‹ Рё С‚РµСЃС‚РѕРІ.
    # ------------------------------------------------------------------
    def stats(self):
        return dict(nodes=self.n, chunk=self.chunk, regions=self.nreg,
                    cols=self.cols, rows=self.rows, entrances=self.ninst,
                    min_weight=self.min_w, grid=self.bool_grid_ok(),
                    cached_regions=len(self.ready_regions()),
                    dirty_regions=len(self.dirty_regions()))

    def bool_grid_ok(self):
        return bool(self.grid_ok)

    def ready_regions(self):
        """Р РµРіРёРѕРЅС‹, С‡СЊСЏ Р»РѕРєР°Р»СЊРЅР°СЏ С‚Р°Р±Р»РёС†Р° СЃРµР№С‡Р°СЃ Р»РµР¶РёС‚ РІ РєСЌС€Рµ СЏРґСЂР°."""
        cdef int r
        cdef list result = []
        if self.row_ready == NULL:
            return result
        for r in range(self.nreg):
            if self.row_ready[r]:
                result.append(r)
        return result

    def dirty_regions(self):
        cdef int r
        cdef list result = []
        if self.dirty == NULL:
            return result
        for r in range(self.nreg):
            if self.dirty[r]:
                result.append(r)
        return result

    def region_id(self, int x, int y):
        return (y//self.chunk)*self.cols+(x//self.chunk)

    def region_of_node(self, int node):
        if node < 0 or node >= self.n:
            return -1
        return self.region_of[node]

    def is_entrance(self, int node):
        if node < 0 or node >= self.n:
            return False
        return self.iof[node] >= 0

    def entrance_count(self, int region):
        if region < 0 or region >= self.nreg:
            return 0
        return self.ioff[region+1]-self.ioff[region]

    def entrance_nodes(self, int region):
        """РРґРµРЅС‚РёС„РёРєР°С‚РѕСЂС‹ РІС…РѕРґРѕРІ СЂРµРіРёРѕРЅР°: РґР»СЏ РґРёР°РіРЅРѕСЃС‚РёРєРё Рё С‚РµСЃС‚РѕРІ."""
        cdef int i
        cdef list result = []
        if region < 0 or region >= self.nreg:
            return result
        for i in range(self.ioff[region], self.ioff[region+1]):
            result.append(self.ids[self.inode[i]])
        return result

    def node_index(self, object node):
        return self.index_of.get(node, -1)

    def ambiguity(self):
        """Р‘С‹Р» Р»Рё РїРѕСЃР»РµРґРЅРёР№ РїРѕРёСЃРє РЅРµРѕРґРЅРѕР·РЅР°С‡РЅС‹Рј РїРѕ РєСЂР°С‚С‡Р°Р№С€РµРјСѓ РїСѓС‚Рё."""
        return bool(self.ambiguous)

    def last_segments(self):
        """РЎРєРѕР»СЊРєРѕ РѕС‚СЂРµР·РєРѕРІ СЃРєР»РµРёР»Р° РёРµСЂР°СЂС…РёСЏ РІ РїРѕСЃР»РµРґРЅРµРј РїРѕРёСЃРєРµ."""
        return self.segments

    # ------------------------------------------------------------------
    # РЎР±РѕСЂРєР°: РєР°СЂС‚Р° -> СЂРµРіРёРѕРЅС‹ -> entrances -> РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ.
    # ------------------------------------------------------------------
    cdef void _build_coords(self, object xs_list, object ys_list):
        cdef int i, nx, ny
        self.xs = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.ys = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.region_of = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.cols = 1
        self.rows = 1
        for i in range(self.n):
            self.xs[i] = xs_list[i]
            self.ys[i] = ys_list[i]
            nx = self.xs[i]//self.chunk+1
            ny = self.ys[i]//self.chunk+1
            if nx > self.cols:
                self.cols = nx
            if ny > self.rows:
                self.rows = ny
        self.nreg = self.cols*self.rows
        for i in range(self.n):
            self.region_of[i] = ((self.ys[i]//self.chunk)*self.cols
                                 +(self.xs[i]//self.chunk))

    cdef int _degree(self, int i):
        cdef object node = self.ids[i]
        cdef object neighbours = self.adj_ref[node]
        cdef int count = 0
        cdef object _ignore
        for _ignore in neighbours:
            count += 1
        return count

    cdef void _build_csr(self):
        """РџРµСЂРµС‡РёС‚Р°С‚СЊ СЂС‘Р±СЂР° РєР°СЂС‚С‹: СЃС‚СЂСѓРєС‚СѓСЂР° Рё РІРµСЃР° РёР· Р¶РёРІРѕРіРѕ ``adj``."""
        cdef int i, k, edges = 0
        cdef int dx, dy
        cdef object node, neighbours, data
        cdef double total = INFINITY
        cdef double value
        self._free_csr()
        for i in range(self.n):
            edges += self._degree(i)
        self.off = <int*>malloc(sizeof(int)*(self.n+1))
        self.adj = <int*>malloc(sizeof(int)*max(2*edges, 1))
        self.wt = <double*>malloc(sizeof(double)*max(2*edges, 1))
        k = 0
        for i in range(self.n):
            self.off[i] = k
            node = self.ids[i]
            neighbours = self.adj_ref[node]
            for neighbour, data in neighbours.items():
                value = data.get('weight', 1)
                if value < 0:
                    raise ValueError('РћС‚СЂРёС†Р°С‚РµР»СЊРЅС‹Р№ РІРµСЃ РјР°СЂС€СЂСѓС‚Р°')
                j = self.index_of[neighbour]
                self.adj[k] = j
                self.wt[k] = value
                if value < total:
                    total = value
                # Р­РІСЂРёСЃС‚РёРєР° В«РјР°РЅС…СЌС‚С‚РµРЅ x min_wВ» РґРѕРїСѓСЃС‚РёРјР° С‚РѕР»СЊРєРѕ РЅР° СЃРµС‚РєРµ:
                # РєР°Р¶РґРѕРµ СЂРµР±СЂРѕ РѕР±СЏР·Р°РЅРѕ СЃРѕРµРґРёРЅСЏС‚СЊ СЃРѕСЃРµРґРЅРёРµ РєР»РµС‚РєРё.
                dx = self.xs[i]-self.xs[j]
                dy = self.ys[i]-self.ys[j]
                if dx < 0:
                    dx = -dx
                if dy < 0:
                    dy = -dy
                if dx+dy != 1:
                    self.grid_ok = 0
                k += 1
        self.off[self.n] = k
        self.min_w = total if k > 0 else 0.0

    cdef void _build_regions(self):
        """РЈР·Р»С‹ РїРѕ СЂРµРіРёРѕРЅР°Рј: roff/rnodes. РџРѕСЂСЏРґРѕРє СѓР·Р»РѕРІ вЂ” РїРѕСЂСЏРґРѕРє РєР°СЂС‚С‹."""
        cdef int i, r
        cdef int* cursor
        self.roff = <int*>malloc(sizeof(int)*(self.nreg+1))
        self.rnodes = <int*>malloc(sizeof(int)*max(self.n, 1))
        for r in range(self.nreg+1):
            self.roff[r] = 0
        for i in range(self.n):
            self.roff[self.region_of[i]+1] += 1
        for r in range(self.nreg):
            self.roff[r+1] += self.roff[r]
        cursor = <int*>malloc(sizeof(int)*(self.nreg+1))
        for r in range(self.nreg+1):
            cursor[r] = self.roff[r]
        for i in range(self.n):
            r = self.region_of[i]
            self.rnodes[cursor[r]] = i
            cursor[r] += 1
        free(cursor)

    cdef bint _has_foreign_neighbour(self, int i):
        cdef int k
        for k in range(self.off[i], self.off[i+1]):
            if self.region_of[self.adj[k]] != self.region_of[i]:
                return True
        return False

    cdef void _index_entrances(self):
        """РџРµСЂРµСЃРѕР±СЂР°С‚СЊ РІС…РѕРґС‹ РїРѕ СЂРµРіРёРѕРЅР°Рј РёР· С‚РµРєСѓС‰РµР№ CSR."""
        cdef int i, r
        cdef int* cursor
        self.iof = <int*>malloc(sizeof(int)*max(self.n, 1))
        for i in range(self.n):
            self.iof[i] = -1
        self.ioff = <int*>malloc(sizeof(int)*(self.nreg+1))
        for r in range(self.nreg+1):
            self.ioff[r] = 0
        for i in range(self.n):
            if self._has_foreign_neighbour(i):
                self.ioff[self.region_of[i]+1] += 1
        for r in range(self.nreg):
            self.ioff[r+1] += self.ioff[r]
        self.ninst = self.ioff[self.nreg]
        self.inode = <int*>malloc(sizeof(int)*max(self.ninst, 1))
        self.ireg = <int*>malloc(sizeof(int)*max(self.ninst, 1))
        cursor = <int*>malloc(sizeof(int)*(self.nreg+1))
        for r in range(self.nreg+1):
            cursor[r] = self.ioff[r]
        for i in range(self.n):
            if not self._has_foreign_neighbour(i):
                continue
            r = self.region_of[i]
            self.inode[cursor[r]] = i
            self.ireg[cursor[r]] = r
            self.iof[i] = cursor[r]
            cursor[r] += 1
        free(cursor)

    cdef void _alloc_scratch(self):
        cdef int slots = self.ninst+2
        self._free_scratch()
        self.ldist = <double*>malloc(sizeof(double)*max(self.n, 1))
        self.lstamp = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.lpred = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.sorder = <int*>malloc(sizeof(int)*max(self.n, 1))
        self.scount = <char*>malloc(max(self.n, 1))
        self.gdist = <double*>malloc(sizeof(double)*max(slots, 1))
        self.gpush = <double*>malloc(sizeof(double)*max(slots, 1))
        self.gpar = <int*>malloc(sizeof(int)*max(slots, 1))
        self.gstamp = <int*>malloc(sizeof(int)*max(slots, 1))
        self.gequal = <char*>malloc(max(slots, 1))
        # РњРµС‚РєРё вЂ” С‡Р°СЃС‚СЊ РїСЂРѕС‚РѕРєРѕР»Р°: РЅРµР·Р°РїРѕР»РЅРµРЅРЅР°СЏ РїР°РјСЏС‚СЊ РјРѕР¶РµС‚ СЃР»СѓС‡Р°Р№РЅРѕ
        # СЃРѕРІРїР°СЃС‚СЊ СЃ С‚РµРєСѓС‰РёРј С‚РѕРєРµРЅРѕРј, РїРѕСЌС‚РѕРјСѓ РѕР±РЅСѓР»СЏРµРј РІСЃРµ С‚Р°Р±Р»РёС†С‹.
        memset(self.lstamp, 0, sizeof(int)*max(self.n, 1))
        memset(self.lpred, 0, sizeof(int)*max(self.n, 1))
        memset(self.sorder, 0, sizeof(int)*max(self.n, 1))
        memset(self.scount, 0, max(self.n, 1))
        memset(self.gdist, 0, sizeof(double)*max(slots, 1))
        memset(self.gpush, 0, sizeof(double)*max(slots, 1))
        memset(self.gpar, 0, sizeof(int)*max(slots, 1))
        memset(self.gstamp, 0, sizeof(int)*max(slots, 1))
        memset(self.gequal, 0, max(slots, 1))
        self.token = 0
        self.gtoken = 0
        self.heap_tape = 0
        self.heap_cap = 64
        self.heap_size = 0
        self.heap_d = <double*>malloc(sizeof(double)*self.heap_cap)
        self.heap_c = <long*>malloc(sizeof(long)*self.heap_cap)
        self.heap_v = <int*>malloc(sizeof(int)*self.heap_cap)

    cdef void _alloc_rows(self):
        cdef int r
        self.row_tab = <double**>malloc(sizeof(double*)*max(self.nreg, 1))
        self.row_ready = <char*>malloc(max(self.nreg, 1))
        self.dirty = <char*>malloc(max(self.nreg, 1))
        for r in range(self.nreg):
            self.row_tab[r] = NULL
            self.row_ready[r] = 0
            self.dirty[r] = 0
        self.dirty_any = 0

    # ------------------------------------------------------------------
    # РљСѓС‡Р° (РєР»СЋС‡, СЃС‡С‘С‚С‡РёРє, СЃР»РѕС‚) вЂ” РєР°Рє РІ route_kernel.
    # ------------------------------------------------------------------
    cdef void _heap_clear(self):
        self.heap_size = 0

    cdef void _grow_heap(self):
        cdef int cap = self.heap_cap*2
        self.heap_d = <double*>realloc(self.heap_d, sizeof(double)*cap)
        self.heap_c = <long*>realloc(self.heap_c, sizeof(long)*cap)
        self.heap_v = <int*>realloc(self.heap_v, sizeof(int)*cap)
        self.heap_cap = cap

    cdef void _push(self, double d, int v):
        cdef int i, parent
        cdef double td
        cdef int tv
        cdef long tc
        if self.heap_size == self.heap_cap:
            self._grow_heap()
        i = self.heap_size
        self.heap_tape += 1
        self.heap_d[i] = d
        self.heap_c[i] = self.heap_tape
        self.heap_v[i] = v
        self.heap_size += 1
        while i > 0:
            parent = (i-1) >> 1
            if self.heap_d[parent] < self.heap_d[i]:
                break
            if self.heap_d[parent] == self.heap_d[i] and self.heap_c[parent] < self.heap_c[i]:
                break
            td = self.heap_d[parent]
            tv = self.heap_v[parent]
            tc = self.heap_c[parent]
            self.heap_d[parent] = self.heap_d[i]
            self.heap_v[parent] = self.heap_v[i]
            self.heap_c[parent] = self.heap_c[i]
            self.heap_d[i] = td
            self.heap_v[i] = tv
            self.heap_c[i] = tc
            i = parent

    cdef void _pop(self, double* out_d, int* out_v):
        cdef int i = 0, left, right, best
        cdef double td
        cdef int tv
        cdef long tc
        out_d[0] = self.heap_d[0]
        out_v[0] = self.heap_v[0]
        self.heap_size -= 1
        if self.heap_size == 0:
            return
        self.heap_d[0] = self.heap_d[self.heap_size]
        self.heap_v[0] = self.heap_v[self.heap_size]
        self.heap_c[0] = self.heap_c[self.heap_size]
        while True:
            left = 2*i+1
            right = left+1
            best = i
            if left < self.heap_size:
                if self._less(left, best):
                    best = left
            if right < self.heap_size:
                if self._less(right, best):
                    best = right
            if best == i:
                break
            td = self.heap_d[best]
            tv = self.heap_v[best]
            tc = self.heap_c[best]
            self.heap_d[best] = self.heap_d[i]
            self.heap_v[best] = self.heap_v[i]
            self.heap_c[best] = self.heap_c[i]
            self.heap_d[i] = td
            self.heap_v[i] = tv
            self.heap_c[i] = tc
            i = best

    cdef bint _less(self, int a, int b):
        if self.heap_d[a] != self.heap_d[b]:
            return self.heap_d[a] < self.heap_d[b]
        return self.heap_c[a] < self.heap_c[b]

    # ------------------------------------------------------------------
    # Р›РѕРєР°Р»СЊРЅС‹Р№ Dijkstra РІРЅСѓС‚СЂРё РѕРґРЅРѕРіРѕ СЂРµРіРёРѕРЅР°.
    # ------------------------------------------------------------------
    cdef void _local_dijkstra(self, int src, int region):
        cdef int i, v, k, u
        cdef double d, nd
        self.token += 1
        for i in range(self.roff[region], self.roff[region+1]):
            v = self.rnodes[i]
            self.lstamp[v] = 0
            self.ldist[v] = _INF
            self.lpred[v] = -1
        self._heap_clear()
        self.ldist[src] = 0.0
        self.lstamp[src] = self.token
        self._push(0.0, src)
        while self.heap_size > 0:
            self._pop(&d, &v)
            if self.lstamp[v] != self.token:
                continue
            self.lstamp[v] = -self.token
            for k in range(self.off[v], self.off[v+1]):
                u = self.adj[k]
                if self.region_of[u] != region:
                    continue
                if self.lstamp[u] == -self.token:
                    continue
                nd = d+self.wt[k]
                if nd < self.ldist[u]:
                    self.ldist[u] = nd
                    self.lpred[u] = v
                    self.lstamp[u] = self.token
                    self._push(nd, u)

    cdef bint _local_reached(self, int node):
        return self.lstamp[node] == -self.token

    cdef double _local_distance(self, int node):
        return self.ldist[node] if self.lstamp[node] == -self.token else _INF

    # ------------------------------------------------------------------
    # Р›РѕРєР°Р»СЊРЅР°СЏ С‚Р°Р±Р»РёС†Р° СЂР°СЃСЃС‚РѕСЏРЅРёР№ РјРµР¶РґСѓ РІС…РѕРґР°РјРё СЂРµРіРёРѕРЅР°.
    # ------------------------------------------------------------------
    cdef double* _row(self, int region):
        cdef int i, j, a, b
        cdef int start = self.ioff[region]
        cdef int count = self.ioff[region+1]-start
        cdef double* row
        if self.row_ready[region]:
            return self.row_tab[region]
        row = <double*>malloc(sizeof(double)*max(count*count, 1))
        for i in range(count):
            a = self.inode[start+i]
            self._local_dijkstra(a, region)
            for j in range(count):
                b = self.inode[start+j]
                row[i*count+j] = self._local_distance(b)
        self.row_tab[region] = row
        self.row_ready[region] = 1
        return row

    # ------------------------------------------------------------------
    # Р­РІСЂРёСЃС‚РёРєР° Рё СЃР»Р°РіР°РµРјС‹Рµ СЃС‚РѕРёРјРѕСЃС‚Рё.
    # ------------------------------------------------------------------
    cdef double _heuristic(self, int region, int goal_region):
        cdef int dx = region % self.cols-goal_region % self.cols
        cdef int dy = region//self.cols-goal_region//self.cols
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        return self.min_w*(dx+dy)

    cdef double _cell_heuristic(self, int node, int goal):
        cdef int dx, dy
        if not self.grid_ok:
            return 0.0
        dx = self.xs[node]-self.xs[goal]
        dy = self.ys[node]-self.ys[goal]
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        return self.min_w*(dx+dy)

    cdef int _slot_region(self, int slot):
        if slot < self.ninst:
            return self.ireg[slot]
        if slot == self.ninst:
            return self.region_of[self.start_index]
        return self.region_of[self.goal_index]

    cdef int _slot_node(self, int slot):
        if slot < self.ninst:
            return self.inode[slot]
        if slot == self.ninst:
            return self.start_index
        return self.goal_index

    cdef void _relax(self, int parent, double nd, int slot, int goal_region):
        cdef double priority
        if self.gstamp[slot] == -self.gtoken:
            return
        if self.gstamp[slot] == self.gtoken:
            if nd < self.gdist[slot]:
                pass
            elif nd == self.gdist[slot]:
                if parent != self.gpar[slot]:
                    self.gequal[slot] = 1
                return
            else:
                return
        self.gstamp[slot] = self.gtoken
        self.gdist[slot] = nd
        self.gpush[slot] = nd
        self.gpar[slot] = parent
        self.gequal[slot] = 0
        priority = nd+self._heuristic(self._slot_region(slot), goal_region)
        self._push(priority, slot)

    # ------------------------------------------------------------------
    # РЎС‚СѓРїРµРЅСЊ 1: A* РїРѕ РіСЂР°С„Сѓ СЂРµРіРёРѕРЅРѕРІ. Р’РѕР·РІСЂР°С‰Р°РµС‚ С†РµРїРѕС‡РєСѓ СЃР»РѕС‚РѕРІ Рё РїСѓС‚СЊ,
    # С‚Рѕ РµСЃС‚СЊ РІРµСЂС…РЅСЋСЋ РіСЂР°РЅРёС†Сѓ СЃС‚РѕРёРјРѕСЃС‚Рё.
    # ------------------------------------------------------------------
    cdef object _hierarchical(self, int start, int goal):
        cdef int goal_region = self.region_of[goal]
        cdef int start_region = self.region_of[start]
        cdef int v, u, k, j, i, r, count, offset, slot
        cdef double d, row_val
        cdef double* row
        cdef double* start_row
        cdef double* goal_row
        cdef int start_slot
        cdef int goal_slot
        cdef int temp_start
        cdef int temp_goal
        cdef list chain
        cdef list path

        self.start_index = start
        self.goal_index = goal
        self.segments = 0
        self.gtoken += 1
        self._heap_clear()
        self.heap_tape = 0

        # РЎС‚Р°СЂС‚ Рё С†РµР»СЊ РІС…РѕРґСЏС‚ РІ РіСЂР°С„ РєР°Рє РІСЂРµРјРµРЅРЅС‹Рµ РІС…РѕРґС‹ СЃРІРѕРёС… СЂРµРіРёРѕРЅРѕРІ,
        # РµСЃР»Рё СЃР°РјРё РЅРµ СЏРІР»СЏСЋС‚СЃСЏ РІС…РѕРґР°РјРё: РёРЅР°С‡Рµ РѕРґРёРЅ Рё С‚РѕС‚ Р¶Рµ СѓР·РµР» Р±С‹Р» Р±С‹
        # РїСЂРµРґСЃС‚Р°РІР»РµРЅ РґРІСѓРјСЏ СЃР»РѕС‚Р°РјРё Рё РґР°Р» Р±С‹ Р»РёС€РЅРёРµ СЂР°РІРЅС‹Рµ РІР°СЂРёР°РЅС‚С‹.
        temp_start = self.ninst
        temp_goal = self.ninst+1
        start_slot = self.iof[start] if self.iof[start] >= 0 else temp_start
        goal_slot = self.iof[goal] if self.iof[goal] >= 0 else temp_goal

        self._local_dijkstra(start, start_region)
        count = self.ioff[start_region+1]-self.ioff[start_region]
        start_row = <double*>malloc(sizeof(double)*max(count, 1))
        for i in range(count):
            start_row[i] = self._local_distance(self.inode[self.ioff[start_region]+i])

        if start_region == goal_region:
            # РћРґРёРЅ СЂРµРіРёРѕРЅ: Р»РѕРєР°Р»СЊРЅС‹Р№ Dijkstra Рё РµСЃС‚СЊ Рё РїСѓС‚СЊ, Рё РіСЂР°РЅРёС†Р°.
            total = self._local_distance(goal)
            free(start_row)
            if total >= _INF:
                return None
            path = []
            if not self._local_path(start, goal, start_region, path):
                return None
            return self._path_cost(path), path

        self._local_dijkstra(goal, goal_region)
        count = self.ioff[goal_region+1]-self.ioff[goal_region]
        goal_row = <double*>malloc(sizeof(double)*max(count, 1))
        for i in range(count):
            goal_row[i] = self._local_distance(self.inode[self.ioff[goal_region]+i])

        self.gstamp[start_slot] = self.gtoken
        self.gdist[start_slot] = 0.0
        self.gpush[start_slot] = 0.0
        self.gpar[start_slot] = -1
        self.gequal[start_slot] = 0
        self._push(self._heuristic(start_region, goal_region), start_slot)
        offset = self.ioff[start_region]
        count = self.ioff[start_region+1]-offset
        for i in range(count):
            if start_slot == offset+i:
                continue
            if start_row[i] < _INF:
                self._relax(start_slot, start_row[i], offset+i, goal_region)

        while self.heap_size > 0:
            self._pop(&d, &v)
            if self.gstamp[v] != self.gtoken:
                continue
            if self.gpush[v] > self.gdist[v]:
                # РЈСЃС‚Р°СЂРµРІС€Р°СЏ Р·Р°РїРёСЃСЊ РєСѓС‡Рё: РІРµСЂС€РёРЅР° СѓР¶Рµ СѓР»СѓС‡С€РµРЅР°.
                continue
            self.gstamp[v] = -self.gtoken
            if v == goal_slot:
                break
            u = self._slot_node(v)
            r = self._slot_region(v)
            if v < self.ninst:
                row = self._row(r)
                offset = self.ioff[r]
                count = self.ioff[r+1]-offset
                for i in range(count):
                    if offset+i == v:
                        continue
                    row_val = row[(v-offset)*count+i]
                    if row_val < _INF:
                        self._relax(v, self.gdist[v]+row_val, offset+i, goal_region)
                if r == goal_region and goal_slot >= self.ninst:
                    row_val = goal_row[v-offset]
                    if row_val < _INF:
                        self._relax(v, self.gdist[v]+row_val, goal_slot, goal_region)
            for k in range(self.off[u], self.off[u+1]):
                j = self.adj[k]
                if self.region_of[j] == r:
                    continue
                slot = self.iof[j]
                if slot < 0:
                    continue
                self._relax(v, self.gdist[v]+self.wt[k], slot, goal_region)

        if self.gstamp[goal_slot] != -self.gtoken:
            free(start_row)
            free(goal_row)
            return None

        chain = []
        slot = goal_slot
        while slot != start_slot:
            chain.append(slot)
            slot = self.gpar[slot]
        chain.append(start_slot)
        chain.reverse()

        path = []
        for i in range(len(chain)-1):
            if not self._append_segment(chain[i], chain[i+1], path):
                free(start_row)
                free(goal_row)
                return None
        free(start_row)
        free(goal_row)
        if not path or path[0] != start or path[len(path)-1] != goal:
            return None
        return self._path_cost(path), path

    cdef bint _append_segment(self, int a, int b, list path):
        cdef int region = self._slot_region(a)
        cdef int node_a = self._slot_node(a)
        cdef int node_b = self._slot_node(b)
        if region == self._slot_region(b):
            return self._local_path(node_a, node_b, region, path)
        if not path or path[len(path)-1] != node_a:
            path.append(node_a)
            path.append(node_b)
        else:
            path.append(node_b)
        self.segments += 1
        return True

    cdef bint _local_path(self, int src, int dst, int region, list out):
        """Р’РѕСЃСЃС‚Р°РЅРѕРІРёС‚СЊ Р»РѕРєР°Р»СЊРЅС‹Р№ РѕС‚СЂРµР·РѕРє РІРЅСѓС‚СЂРё СЂРµРіРёРѕРЅР°."""
        cdef int v
        cdef list chunk
        if src == dst:
            if not out or out[len(out)-1] != src:
                out.append(src)
            return True
        self._local_dijkstra(src, region)
        if not self._local_reached(dst):
            return False
        chunk = []
        v = dst
        while v != -1:
            chunk.append(v)
            if v == src:
                break
            v = self.lpred[v]
        if v != src:
            return False
        chunk.reverse()
        for i in range(len(chunk)):
            v = chunk[i]
            if not out or out[len(out)-1] != v:
                out.append(v)
        self.segments += 1
        return True

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # РўРѕС‡РєР° РІС…РѕРґР° РїРѕРёСЃРєР°: РёРµСЂР°СЂС…РёСЏ -> РіСЂР°РЅРёС†Р° -> С‚РѕС‡РЅС‹Р№ РїСЂРѕС…РѕРґ.
    # ------------------------------------------------------------------
    cdef object _search(self, int start, int goal):
        cdef object bound
        cdef object exact
        cdef double upper = INFINITY
        if start == goal:
            return 0.0, [start]
        self.ambiguous = 0
        if self.dirty_any:
            self._refresh()
        # РЎС‚СѓРїРµРЅСЊ 1: РёРµСЂР°СЂС…РёСЏ. Р•С‘ РїСѓС‚СЊ вЂ” РґРѕРїСѓСЃС‚РёРјР°СЏ РІРµСЂС…РЅСЏСЏ РіСЂР°РЅРёС†Р° Рё, С‡С‚Рѕ
        # РІР°Р¶РЅРµРµ, РєРѕСЂРёРґРѕСЂ: СЃС‚РѕРёРјРѕСЃС‚СЊ, РІС‹С€Рµ РєРѕС‚РѕСЂРѕР№ РёСЃРєР°С‚СЊ Р±РµСЃСЃРјС‹СЃР»РµРЅРЅРѕ.
        bound = self._hierarchical(start, goal)
        if bound is not None:
            upper = bound[0]
        # РЎС‚СѓРїРµРЅСЊ 2: С‚РѕС‡РЅС‹Р№ РѕР±С…РѕРґ РІ РїРѕСЂСЏРґРєРµ РїСЂРµР¶РЅРµРіРѕ Dijkstra. РћРЅ РґР°С‘С‚ СЂРѕРІРЅРѕ
        # С‚РѕС‚ РїСѓС‚СЊ, С‡С‚Рѕ РІРµСЂРЅСѓР» Р±С‹ ``single_source``, РІРєР»СЋС‡Р°СЏ РІС‹Р±РѕСЂ РїСЂРµРґРєР° РїСЂРё
        # СЂР°РІРЅС‹С… РґРёСЃС‚Р°РЅС†РёСЏС…, РїРѕСЌС‚РѕРјСѓ РѕС‚РґРµР»СЊРЅР°СЏ РїСЂРѕРІРµСЂРєР° РµРґРёРЅСЃС‚РІРµРЅРЅРѕСЃС‚Рё РЅРµ
        # РЅСѓР¶РЅР°: РЅРµРѕРґРЅРѕР·РЅР°С‡РЅРѕСЃС‚СЊ СЂР°Р·СЂРµС€Р°РµС‚СЃСЏ С‚Р°Рє Р¶Рµ, РєР°Рє СЂР°РЅСЊС€Рµ.
        exact = self._exact(start, goal, upper)
        if exact is None:
            return None
        if bound is not None and exact[0] > upper:
            # РРµСЂР°СЂС…РёС‡РµСЃРєР°СЏ РіСЂР°РЅРёС†Р° РѕРєР°Р·Р°Р»Р°СЃСЊ РЅРµРґРѕРїСѓСЃС‚РёРјРѕР№: РґРѕРІРµСЂСЏС‚СЊ РµР№ РЅРµР»СЊР·СЏ.
            self.ambiguous = 1
            return None
        return exact

    # ------------------------------------------------------------------
    # РЎС‚СѓРїРµРЅСЊ 3: С‚РѕС‡РЅС‹Р№ РѕРіСЂР°РЅРёС‡РµРЅРЅС‹Р№ Dijkstra РІ РїРѕСЂСЏРґРєРµ route_kernel.
    # ------------------------------------------------------------------
    cdef object _exact(self, int start, int goal, double upper):
        """РўРѕС‡РЅС‹Р№ РїСѓС‚СЊ Рё СЃС‚РѕРёРјРѕСЃС‚СЊ СЂРѕРІРЅРѕ РєР°Рє ``route_kernel.single_source``.

        РџРѕСЂСЏРґРѕРє РєСѓС‡Рё вЂ” ``(СЂР°СЃСЃС‚РѕСЏРЅРёРµ, СЃС‡С‘С‚С‡РёРє)``, СЃС‡С‘С‚С‡РёРє СЂР°СЃС‚С‘С‚ РЅР° РєР°Р¶РґРѕР№
        РІСЃС‚Р°РІРєРµ, РІРµСЂС€РёРЅР° С„РёРєСЃРёСЂСѓРµС‚СЃСЏ РїСЂРё РїРµСЂРІРѕРј РёР·РІР»РµС‡РµРЅРёРё, РїСЂРµРґРѕРє Р±РµСЂС‘С‚СЃСЏ РёР·
        РїРµСЂРІРѕР№ СЂРµР»Р°РєСЃР°С†РёРё. Р­С‚Рѕ Р±СѓРєРІР°Р»СЊРЅРѕ С‚РѕС‚ Р¶Рµ РїСЂРѕС‚РѕРєРѕР», С‡С‚Рѕ Сѓ РїСЂРµР¶РЅРµРіРѕ СЏРґСЂР°,
        РїРѕСЌС‚РѕРјСѓ РїСЂРё СЂР°РІРЅС‹С… РґРёСЃС‚Р°РЅС†РёСЏС… РІС‹Р±СЂР°РЅРЅС‹Р№ РїСЂРµРґРѕРє СЃРѕРІРїР°РґР°РµС‚, Р° Р·РЅР°С‡РёС‚
        СЃРѕРІРїР°РґР°РµС‚ Рё РїСѓС‚СЊ.

        РћР±СЂРµР·РєР° ``upper`` (СЃС‚РѕРёРјРѕСЃС‚СЊ РёРµСЂР°СЂС…РёС‡РµСЃРєРѕРіРѕ РїСѓС‚Рё) РЅРµ РјРµРЅСЏРµС‚ РїРѕСЂСЏРґРѕРє:
        РїСЂРµРґРѕРє РІРµСЂС€РёРЅС‹ РІСЃРµРіРґР° РЅРµ РґР°Р»СЊС€Рµ РЅРµС‘, РїРѕСЌС‚РѕРјСѓ РІРµСЂС€РёРЅС‹ СЃ СЂР°СЃСЃС‚РѕСЏРЅРёРµРј
        Р±РѕР»СЊС€Рµ ``upper`` РЅРµ РјРѕРіСѓС‚ РїРѕРІР»РёСЏС‚СЊ РЅР° РїСѓС‚СЊ РЅРµ РґРѕСЂРѕР¶Рµ ``upper``.

        Р”РѕРїСѓСЃРє РЅСѓР¶РµРЅ С‚РѕР»СЊРєРѕ РёР·-Р·Р° РїРѕСЃР»РµРґРЅРµРіРѕ Р±РёС‚Р°: РіСЂР°РЅРёС†Р° РїРѕСЃС‡РёС‚Р°РЅР° СЃРІС‘СЂС‚РєРѕР№
        РїРѕ РёРµСЂР°СЂС…РёС‡РµСЃРєРѕРјСѓ РїСѓС‚Рё, Р° ``g+h`` РЅР° СѓР·Р»Рµ РїСѓС‚Рё вЂ” РґСЂСѓРіРѕР№ СЃРІС‘СЂС‚РєРѕР№, Рё
        С‚РµРѕСЂРµС‚РёС‡РµСЃРєРѕРµ РЅРµСЂР°РІРµРЅСЃС‚РІРѕ РјРѕР¶РµС‚ РЅР°СЂСѓС€РёС‚СЊСЃСЏ РЅР° РѕРєСЂСѓРіР»РµРЅРёРё.
        """
        cdef int v, u, k, i
        cdef double d, nd, total
        cdef double slack = upper+abs(upper)*1e-12+1e-12
        cdef list path

        self.token += 1
        self._heap_clear()
        self.heap_tape = 0
        self.ldist[start] = 0.0
        self.lpred[start] = -1
        self.lstamp[start] = self.token
        self._push(0.0, start)

        while self.heap_size > 0:
            self._pop(&d, &v)
            if self.lstamp[v] == -self.token:
                continue
            self.lstamp[v] = -self.token
            self.ldist[v] = d
            if v == goal:
                break
            for k in range(self.off[v], self.off[v+1]):
                u = self.adj[k]
                if self.lstamp[u] == -self.token:
                    continue
                nd = d+self.wt[k]
                if nd > slack:
                    continue
                # РћР±СЂРµР·РєР° РєРѕСЂРёРґРѕСЂР°: СѓР·РµР», Сѓ РєРѕС‚РѕСЂРѕРіРѕ g+h Р±РѕР»СЊС€Рµ РіСЂР°РЅРёС†С‹, РЅРµ
                # Р»РµР¶РёС‚ РЅРё РЅР° РѕРґРЅРѕРј РїСѓС‚Рё РЅРµ РґРѕСЂРѕР¶Рµ РіСЂР°РЅРёС†С‹, РїРѕСЌС‚РѕРјСѓ РІ РїСѓС‚СЊ РѕРЅ
                # РЅРµ РїРѕРїР°РґС‘С‚. РЈР·Р»С‹ СЃР°РјРѕРіРѕ РїСѓС‚Рё РёРјРµСЋС‚ g+h <= СЃС‚РѕРёРјРѕСЃС‚Рё РїСѓС‚Рё,
                # Р·РЅР°С‡РёС‚ РЅРµ РѕС‚СЃРµРєР°СЋС‚СЃСЏ, Р° РѕС‚РЅРѕСЃРёС‚РµР»СЊРЅС‹Р№ РїРѕСЂСЏРґРѕРє РѕСЃС‚Р°РІС€РёС…СЃСЏ
                # РІСЃС‚Р°РІРѕРє СЃРѕС…СЂР°РЅСЏРµС‚СЃСЏ: РїСЂРѕРїСѓСЃРє РІСЃС‚Р°РІРєРё РЅРµ РјРµРЅСЏРµС‚ РїРѕСЂСЏРґРѕРє
                # РѕСЃС‚Р°Р»СЊРЅС‹С…. РџРѕСЌС‚РѕРјСѓ РІС‹Р±РѕСЂ РїСЂРµРґРєР° РїСЂРё СЂР°РІРЅС‹С… РґРёСЃС‚Р°РЅС†РёСЏС… С‚РѕС‚
                # Р¶Рµ, С‡С‚Рѕ Сѓ РїРѕР»РЅРѕРіРѕ Dijkstra. Р”РѕРїСѓСЃРє ``slack`` РЅСѓР¶РµРЅ РїРѕС‚РѕРјСѓ,
                # С‡С‚Рѕ g+h РЅР° СѓР·Р»Рµ РїСѓС‚Рё РјРѕР¶РµС‚ РЅР° РїРѕСЃР»РµРґРЅРµРј Р±РёС‚Рµ РїСЂРµРІС‹СЃРёС‚СЊ
                # РіСЂР°РЅРёС†Сѓ: РѕРЅР° РїРѕСЃС‡РёС‚Р°РЅР° РґСЂСѓРіРѕР№ СЃРІС‘СЂС‚РєРѕР№.
                if nd+self._cell_heuristic(u, goal) > slack:
                    continue
                if self.lstamp[u] != self.token or nd < self.ldist[u]:
                    self.ldist[u] = nd
                    self.lpred[u] = v
                    self.lstamp[u] = self.token
                    self._push(nd, u)

        if self.lstamp[goal] != -self.token:
            return None
        total = self.ldist[goal]
        path = []
        v = goal
        while v != -1:
            path.append(v)
            v = self.lpred[v]
        path.reverse()
        if path[0] != start:
            return None
        return total, path

    cdef double _edge_weight(self, int a, int b):
        cdef int k
        for k in range(self.off[a], self.off[a+1]):
            if self.adj[k] == b:
                return self.wt[k]
        return _INF

    cdef double _path_cost(self, list path):
        """РЎС‚РѕРёРјРѕСЃС‚СЊ РїСѓС‚Рё Р»РµРІРѕР№ СЃРІС‘СЂС‚РєРѕР№ вЂ” С‚РµРј Р¶Рµ РїРѕСЂСЏРґРєРѕРј, С‡С‚Рѕ Сѓ Dijkstra."""
        cdef int i
        cdef double total = 0.0
        cdef double w
        for i in range(len(path)-1):
            w = self._edge_weight(path[i], path[i+1])
            if w >= _INF:
                return _INF
            total = total+w
        return total

    # ------------------------------------------------------------------
    # РРЅРІР°Р»РёРґР°С†РёСЏ Рё С‚РѕС‡РµС‡РЅРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ.
    # ------------------------------------------------------------------
    cdef void _refresh(self):
        cdef int r
        if not self.dirty_any:
            return
        self.grid_ok = 1
        self._build_csr()
        self._index_entrances()
        self._alloc_scratch()
        for r in range(self.nreg):
            if self.dirty[r] and self.row_tab[r] != NULL:
                free(self.row_tab[r])
                self.row_tab[r] = NULL
                self.row_ready[r] = 0
            self.dirty[r] = 0
        self.dirty_any = 0

    def invalidate(self, object nodes):
        """РџРѕРјРµС‚РёС‚СЊ СЂРµРіРёРѕРЅС‹ РїРµСЂРµС‡РёСЃР»РµРЅРЅС‹С… СѓР·Р»РѕРІ; С‡РёСЃС‚С‹Рµ С‚Р°Р±Р»РёС†С‹ РѕСЃС‚Р°СЋС‚СЃСЏ."""
        cdef int i
        for node in nodes:
            i = self.index_of.get(node, -1)
            if i < 0:
                continue
            self.dirty[self.region_of[i]] = 1
            self.dirty_any = 1


cdef inline object _to_nodes(RegionGraph graph, object result):
    """РџРµСЂРµРІРµСЃС‚Рё РёРЅРґРµРєСЃС‹ РІРЅСѓС‚СЂРµРЅРЅРµРіРѕ РїСѓС‚Рё РІ РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂС‹ СѓР·Р»РѕРІ РєР°СЂС‚С‹."""
    cdef list path
    cdef list nodes
    cdef int i
    if result is None:
        return None
    path = result[1]
    nodes = [graph.ids[path[i]] for i in range(len(path))]
    return result[0], nodes


def build(object adj, object ids, object xs, object ys, int chunk=32):
    """РЎРѕР±СЂР°С‚СЊ RegionGraph: РєР°СЂС‚Р° -> СЂРµРіРёРѕРЅС‹ -> entrances -> РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ."""
    cdef RegionGraph graph = RegionGraph()
    if chunk < 1:
        raise ValueError('Р Р°Р·РјРµСЂ СЂРµРіРёРѕРЅР° РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РїРѕР»РѕР¶РёС‚РµР»СЊРЅС‹Рј')
    if not ids:
        raise ValueError('РџСѓСЃС‚Р°СЏ РєР°СЂС‚Р°')
    graph.ids = ids
    graph.adj_ref = adj
    graph.index_of = {node: i for i, node in enumerate(ids)}
    if len(graph.index_of) != len(ids):
        raise ValueError('РџРѕРІС‚РѕСЂСЏСЋС‰РёР№СЃСЏ СѓР·РµР» РєР°СЂС‚С‹')
    graph.n = len(ids)
    graph.chunk = chunk
    graph._build_coords(xs, ys)
    graph._build_csr()
    graph._build_regions()
    graph._index_entrances()
    graph._alloc_scratch()
    graph._alloc_rows()
    return graph


def search(RegionGraph graph, object start, object goal):
    """РўРѕС‡РЅС‹Р№ РїСѓС‚СЊ Рё СЃС‚РѕРёРјРѕСЃС‚СЊ РјРµР¶РґСѓ РґРІСѓРјСЏ СѓР·Р»Р°РјРё С‡РµСЂРµР· РіСЂР°С„ СЂРµРіРёРѕРЅРѕРІ.

    ``start``/``goal`` вЂ” РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂС‹ СѓР·Р»РѕРІ РєР°СЂС‚С‹ (РєР°Рє РІ ``graph._adj``).
    Р’РѕР·РІСЂР°С‰Р°РµС‚ ``(cost, path)`` РёР»Рё ``None``: РїСѓС‚Рё РЅРµС‚, Р»РёР±Рѕ РєСЂР°С‚С‡Р°Р№С€РёР№ РїСѓС‚СЊ
    РЅРµ РµРґРёРЅСЃС‚РІРµРЅРµРЅ, Рё С‚РѕРіРґР° РїСЂРµРґРєР° РѕР±СЏР·Р°РЅ РІС‹Р±СЂР°С‚СЊ РїСЂРµР¶РЅРёР№ Dijkstra.
    """
    cdef int a = graph.index_of.get(start, -1)
    cdef int b = graph.index_of.get(goal, -1)
    if a < 0 or b < 0:
        return None
    return _to_nodes(graph, graph._search(a, b))


def invalidate(RegionGraph graph, object nodes):
    """РџРѕРјРµС‚РёС‚СЊ СЂРµРіРёРѕРЅС‹ РїРµСЂРµС‡РёСЃР»РµРЅРЅС‹С… СѓР·Р»РѕРІ (РјРѕРґСѓР»РµРІР°СЏ РѕР±С‘СЂС‚РєР° РјРµС‚РѕРґР°)."""
    graph.invalidate(nodes)


def ready_regions(RegionGraph graph):
    """Р РµРіРёРѕРЅС‹ СЃ РїРѕСЃС‡РёС‚Р°РЅРЅРѕР№ Р»РѕРєР°Р»СЊРЅРѕР№ С‚Р°Р±Р»РёС†РµР№."""
    return graph.ready_regions()


def dirty_regions(RegionGraph graph):
    """Р РµРіРёРѕРЅС‹, РїРѕРјРµС‡РµРЅРЅС‹Рµ Рє РїРµСЂРµСЃР±РѕСЂРєРµ."""
    return graph.dirty_regions()


def region_of_node(RegionGraph graph, object node):
    """Р РµРіРёРѕРЅ СѓР·Р»Р° РєР°СЂС‚С‹ РёР»Рё -1."""
    cdef int index = graph.index_of.get(node, -1)
    if index < 0:
        return -1
    return graph.region_of_node(index)


def stats(RegionGraph graph):
    """Р”РёР°РіРЅРѕСЃС‚РёРєР° СЂР°Р·Р±РёРµРЅРёСЏ Рё РєСЌС€РµР№ СЏРґСЂР°."""
    return graph.stats()


def ambiguity(RegionGraph graph):
    """Р‘С‹Р» Р»Рё РїРѕСЃР»РµРґРЅРёР№ РїРѕРёСЃРє РЅРµРѕРґРЅРѕР·РЅР°С‡РЅС‹Рј РїРѕ РєСЂР°С‚С‡Р°Р№С€РµРјСѓ РїСѓС‚Рё."""
    return graph.ambiguity()


def search_index(RegionGraph graph, int start, int goal):
    """РўРѕ Р¶Рµ РїРѕ РІРЅСѓС‚СЂРµРЅРЅРёРј РЅРѕРјРµСЂР°Рј СѓР·Р»РѕРІ, РїСѓС‚СЊ вЂ” РёРЅРґРµРєСЃР°РјРё: РґР»СЏ С‚РµСЃС‚РѕРІ."""
    if start < 0 or start >= graph.n or goal < 0 or goal >= graph.n:
        return None
    return graph._search(start, goal)


def bound(RegionGraph graph, object start, object goal):
    """РўРѕР»СЊРєРѕ РёРµСЂР°СЂС…РёС‡РµСЃРєР°СЏ СЃС‚СѓРїРµРЅСЊ: РїСѓС‚СЊ Рё СЃС‚РѕРёРјРѕСЃС‚СЊ Р±РµР· СѓС‚РѕС‡РЅРµРЅРёСЏ."""
    cdef int a = graph.index_of.get(start, -1)
    cdef int b = graph.index_of.get(goal, -1)
    if a < 0 or b < 0:
        return None
    if graph.dirty_any:
        graph._refresh()
    return _to_nodes(graph, graph._hierarchical(a, b))
