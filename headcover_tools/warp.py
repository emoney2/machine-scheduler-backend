import struct
def getwarp(so):
    def dbl_after(key, start=0):
        j=so.find(key,start)+len(key); t=so[j:j+4]
        if t==b'doub': return struct.unpack('>d',so[j+4:j+12])[0], j+12
        if t==b'long': return struct.unpack('>i',so[j+4:j+8])[0], j+8
        if t==b'UntF': return struct.unpack('>d',so[j+8:j+16])[0], j+16
    def unfl(key,start):
        j=so.find(key+b'UnFl',start)+len(key); n=struct.unpack('>I',so[j+8:j+12])[0]
        return list(struct.unpack('>%dd'%n,so[j+12:j+12+8*n])), j+12+8*n
    w0=so.find(b'warpStyle')
    out={}
    b=so.find(b'bounds',w0)
    out['bounds']=[dbl_after(k,b)[0] for k in (b'Top ',b'Left',b'Btom',b'Rght')]
    if so.find(b'deformNumRows',w0)>0:
        out['rows']=dbl_after(b'deformNumRows',w0)[0]; out['cols']=dbl_after(b'deformNumCols',w0)[0]
    else:
        out['rows']=out['cols']=4; T,Lf,B,R=out['bounds']; out['sx']=[Lf,R]; out['sy']=[T,B]
    q=so.find(b'quiltSliceX',w0)
    if q>0:
        out['sx'],p=unfl(b'quiltSliceX',q); out['sy'],p=unfl(b'quiltSliceY',p)
    m=so.find(b'meshPoints',w0)
    out['h'],p=unfl(b'Hrzn',m); out['v'],p=unfl(b'Vrtc',p)
    t=so.find(b'Trnf'); 
    j=so.find(b'VlLs',t); n=struct.unpack('>I',so[j+4:j+8])[0]; vals=[]; pp=j+8
    for _ in range(n): pp+=4; vals.append(struct.unpack('>d',so[pp:pp+8])[0]); pp+=8
    out['trnf']=vals
    j=so.find(b'Wdthdoub',so.find(b'Sz  '))+8; out['W']=struct.unpack('>d',so[j:j+8])[0]
    j=so.find(b'Hghtdoub',so.find(b'Sz  '))+8; out['H']=struct.unpack('>d',so[j:j+8])[0]
    return out
