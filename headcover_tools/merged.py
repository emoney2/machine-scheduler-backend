import struct, numpy as np, sys
from .psdread import packbits
def read_merged(path):
    b=open(path,'rb').read(); p=0
    sig,ver=struct.unpack_from('>4sH',b,0); psb=ver==2
    ch,H,W,depth,mode=struct.unpack_from('>HIIHH',b,12); p=26
    p+=4+struct.unpack_from('>I',b,p)[0]
    p+=4+struct.unpack_from('>I',b,p)[0]
    if psb: p+=8+struct.unpack_from('>Q',b,p)[0]
    else: p+=4+struct.unpack_from('>I',b,p)[0]
    comp=struct.unpack_from('>H',b,p)[0]; p+=2

    if comp==1:
        cnt=ch*H; fmt='>%d%s'%(cnt,'I' if psb else 'H'); rc=struct.unpack_from(fmt,b,p); p+=struct.calcsize(fmt)
        chans=[]
        for c in range(ch):
            rows=[]
            for r in range(H):
                n=rc[c*H+r]; rows.append(packbits(b[p:p+n],W)); p+=n
            chans.append(np.frombuffer(b''.join(rows),np.uint8).reshape(H,W))
    else:
        chans=[np.frombuffer(b[p+c*W*H:p+(c+1)*W*H],np.uint8).reshape(H,W) for c in range(ch)]
    return np.dstack(chans[:3])
if __name__=='__main__':
    from PIL import Image
    a=read_merged(sys.argv[1]); Image.fromarray(a).save(sys.argv[2]); print(a.shape)
