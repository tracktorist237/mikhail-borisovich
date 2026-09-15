"""Send Space only to the currently active, owned Firefox X11 window.

DOM code must prepare and guard the target button first. No pointer events.
"""
import ctypes as C


def space_to_firefox(pid):
    x = C.CDLL('libX11.so.6')
    xt = C.CDLL('libXtst.so.6')
    pointer, ulong, integer = C.c_void_p, C.c_ulong, C.c_int
    x.XOpenDisplay.argtypes=[C.c_char_p]; x.XOpenDisplay.restype=pointer
    x.XDefaultRootWindow.argtypes=[pointer]; x.XDefaultRootWindow.restype=ulong
    x.XInternAtom.argtypes=[pointer,C.c_char_p,integer]; x.XInternAtom.restype=ulong
    x.XGetWindowProperty.argtypes=[pointer,ulong,ulong,C.c_long,C.c_long,integer,ulong,
        C.POINTER(ulong),C.POINTER(integer),C.POINTER(ulong),C.POINTER(ulong),C.POINTER(pointer)]
    x.XGetWindowProperty.restype=integer
    x.XFree.argtypes=[pointer]; x.XFree.restype=integer
    x.XGetInputFocus.argtypes=[pointer,C.POINTER(ulong),C.POINTER(integer)]
    x.XQueryTree.argtypes=[pointer,ulong,C.POINTER(ulong),C.POINTER(ulong),C.POINTER(pointer),C.POINTER(C.c_uint)]
    x.XKeysymToKeycode.argtypes=[pointer,ulong]; x.XKeysymToKeycode.restype=C.c_ubyte
    x.XSync.argtypes=[pointer,integer]
    x.XCloseDisplay.argtypes=[pointer]
    xt.XTestFakeKeyEvent.argtypes=[pointer,C.c_uint,integer,ulong]
    xt.XTestFakeKeyEvent.restype=integer
    display=x.XOpenDisplay(None)
    if not display:raise RuntimeError('X11 display unavailable')
    def prop(window,name):
        atom=x.XInternAtom(display,name.encode(),True)
        kind,fmt,n,left,data=ulong(),integer(),ulong(),ulong(),pointer()
        status=x.XGetWindowProperty(display,window,atom,0,1,False,0,
            C.byref(kind),C.byref(fmt),C.byref(n),C.byref(left),C.byref(data))
        try:
            if status or fmt.value!=32 or n.value!=1:return None
            return C.cast(data,C.POINTER(ulong))[0]
        finally:
            if data:x.XFree(data)
    try:
        root=x.XDefaultRootWindow(display)
        active=prop(root,'_NET_ACTIVE_WINDOW')
        if not active or prop(active,'_NET_WM_PID')!=pid:
            raise RuntimeError('Active window is not the owned Firefox; no key sent')
        focus,revert=ulong(),integer()
        x.XGetInputFocus(display,C.byref(focus),C.byref(revert))
        current=focus.value
        while current not in (0,1,root,active):
            tree_root,parent,count,children=ulong(),ulong(),C.c_uint(),pointer()
            if not x.XQueryTree(display,current,C.byref(tree_root),C.byref(parent),C.byref(children),C.byref(count)):
                raise RuntimeError('Cannot verify Firefox keyboard focus')
            if children:x.XFree(children)
            current=parent.value
        if current!=active:raise RuntimeError('Keyboard focus is outside owned Firefox')
        key=x.XKeysymToKeycode(display,0x20)
        if not key:raise RuntimeError('Space key unavailable')
        if not xt.XTestFakeKeyEvent(display,key,True,0):raise RuntimeError('XTest key press failed')
        xt.XTestFakeKeyEvent(display,key,False,0)
        x.XSync(display,False)
    finally:
        x.XCloseDisplay(display)
