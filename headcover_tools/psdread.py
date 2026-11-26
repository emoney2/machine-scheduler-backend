import struct, sys, zlib, numpy as np
from PIL import Image
def packbits(data, n):
    out=bytearray(); i=0
    while len(out)<n and i<len(data):
        h=data[i]; i+=1
        if h<128: out+=data[i:i+h+1]; i+=h+1
        elif h>128: out+=bytes([data[i]])*(257-h); i+=1
    return bytes(out[:n])
class R:
    def __init__(s,b,p=0): s.b=b; s.p=p
    def u(s,f): v=struct.unpack_from('>'+f,s.b,s.p); s.p+=struct.calcsize('>'+f); return v if len(v)>1 else v[0]
    def raw(s,n): v=s.b[s.p:s.p+n]; s.p+=n; return v
def parse(path, extract=True, verbose=False):
    b=open(path,'rb').read(); r=R(b)
    sig,ver=r.u('4sH'); r.raw(6); ch,H,W,depth,mode=r.u('HIIHH'); psb=ver==2
    if verbose: print('doc',W,H,'ch',ch,'depth',depth,'mode',mode,'psb',psb)
    r.p+=4+r.u('I')          # color mode
    r.p+=4+r.u('I')          # resources (skip)
    lmlen=r.u('Q' if psb else 'I'); lmend=r.p+lmlen
    lilen=r.u('Q' if psb else 'I'); liend=r.p+lilen
    cnt=r.u('h'); cnt=abs(cnt); layers=[]
    for i in range(cnt):
        t,l,bt,rt=r.u('iiii'); nch=r.u('H'); chans=[]
        for c in range(nch): cid=r.u('h'); ln=r.u('Q' if psb else 'I'); chans.append((cid,ln))
        bsig,blend,op,clip,flags,_=r.u('4s4sBBBB'); exl=r.u('I'); exend=r.p+exl
        ml=r.u('I'); mask=None
        if ml: mt,mlft,mb,mr=struct.unpack_from('>iiii',b,r.p); mask=(mt,mlft,mb,mr); 
        r.p+=ml; bl=r.u('I'); r.p+=bl
        nl=r.u('B'); name=r.raw(nl).decode('latin1'); r.p+= (4-((nl+1)%4))%4
        extra={}
        while r.p<exend-11:
            s8,key=r.u('4s4s'); 
            big = psb and key in (b'LMsk',b'Lr16',b'Lr32',b'Layr',b'Mt16',b'Mt32',b'Mtrn',b'Alph',b'FMsk',b'lnk2',b'FEid',b'FXid',b'PxSD')
            ln=r.u('Q' if big else 'I'); d=r.raw(ln); 
            if ln%2 and False: pass
            extra[key]=d
            if key==b'luni': n=struct.unpack('>I',d[:4])[0]; name=d[4:4+2*n].decode('utf-16-be')
        r.p=exend
        sect=None
        if b'lsct' in extra: sect=struct.unpack('>I',extra[b'lsct'][:4])[0]
        layers.append(dict(name=name,rect=(t,l,bt,rt),chans=chans,blend=blend.decode(),op=op,clip=clip,hidden=bool(flags&2),sect=sect,mask=mask,keys=[k.decode() for k in extra],extra=extra))
    for L in layers:
        t,l,bt,rt=L['rect']; h,w=bt-t,rt-l; data={}
        for cid,ln in L['chans']:
            start=r.p; comp=r.u('H')
            if extract and cid in (0,1,2,-1,-2) and ln>2:
                hh,ww=(h,w)
                if cid==-2 and L['mask']: mt,ml_,mb,mr=L['mask']; hh,ww=mb-mt,mr-ml_
                if hh>0 and ww>0:
                    if comp==0: arr=np.frombuffer(r.raw(hh*ww),np.uint8)
                    elif comp==1:
                        rc=[r.u('I' if psb else 'H') for _ in range(hh)]; out=bytearray()
                        for c in rc: out+=packbits(r.raw(c),ww)
                        arr=np.frombuffer(bytes(out),np.uint8)
                    else:
                        raw=zlib.decompress(r.raw(ln-2)); arr=np.frombuffer(raw,np.uint8).copy()
                        if comp==3:
                            a=arr.reshape(hh,ww)
                            arr=np.cumsum(a,axis=1,dtype=np.uint8).reshape(-1)
                    try: data[cid]=arr.reshape(hh,ww)
                    except Exception as e: print('reshape fail',L['name'],cid,e)
            r.p=start+ln
        L['data']=data
    return W,H,layers
