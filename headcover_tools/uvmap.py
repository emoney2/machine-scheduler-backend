import numpy as np
from .warp import getwarp
MODE='curve'
def bern(t): return np.stack([(1-t)**3,3*t*(1-t)**2,3*t*t*(1-t),t**3],-1)
def map_layer(so, W, H, outW, outH, ss=3):
    """returns uv (outH,outW,2) source coords in content space (W x H), nan where none"""
    uv=np.full((outH,outW,2),np.nan,np.float32)
    style = b'warpCustom' in so[so.find(b'warpStyle'):so.find(b'warpStyle')+60]
    t=getwarp(so) if style else None
    import struct
    if not style:
        # trnf only
        j=so.find(b'VlLs',so.find(b'Trnf')); vals=[]; pp=j+8
        for _ in range(8): pp+=4; vals.append(struct.unpack('>d',so[pp:pp+8])[0]); pp+=8
        x0,y0,x1,_,_,y1,_,_=vals
        ys,xs=np.mgrid[0:outH,0:outW]+0.5
        u=(xs-x0)/(x1-x0)*W; v=(ys-y0)/(y1-y0)*H
        m=(u>=0)&(u<W)&(v>=0)&(v<H); uv[...,0]=np.where(m,u,np.nan); uv[...,1]=np.where(m,v,np.nan); return uv
    rows,cols=int(t['rows']),int(t['cols']); sx,sy=t['sx'],t['sy']
    P=np.stack([np.array(t['h']),np.array(t['v'])],-1).reshape(rows,cols,2)
    # mesh space -> content space normalize by slice extents
    mx0,mx1=sx[0],sx[-1]; my0,my1=sy[0],sy[-1]
    P[...,0]=(P[...,0]-mx0)/(mx1-mx0)*W; P[...,1]=(P[...,1]-my0)/(my1-my0)*H
    S_x=[(x-mx0)/(mx1-mx0)*W for x in sx]; S_y=[(y-my0)/(my1-my0)*H for y in sy]
    qx0,qy0,qx1,_,_,qy1,_,_=t['trnf']
    npx=(cols-1)//3; npy=(rows-1)//3
    # bbox of the warped surface (curve samples), in content space
    pts=[]
    for pj in range(npy):
        for pi in range(npx):
            C=P[3*pj:3*pj+4,3*pi:3*pi+4]; uu=np.linspace(0,1,60); o=np.einsum('vj,ui,jic->vuc',bern(uu),bern(uu),C); pts.append(o.reshape(-1,2))
    pts=np.concatenate(pts); bx0,by0=pts.min(0); bx1,by1=pts.max(0)
    if MODE=='cp': bx0,by0=P.reshape(-1,2).min(0); bx1,by1=P.reshape(-1,2).max(0)
    for pj in range(npy):
        for pi in range(npx):
            C=P[3*pj:3*pj+4,3*pi:3*pi+4]          # 4x4x2
            # sample density
            n=int(max(outW,outH)*ss/ max(1,min(npx,npy)))
            u=np.linspace(0,1,n); v=np.linspace(0,1,n)
            Bu=bern(u); Bv=bern(v)
            out=np.einsum('vj,ui,jic->vuc',Bv,Bu,C)  # (n,n,2) content-space warped
            if MODE=='content': ox=qx0+out[...,0]/W*(qx1-qx0); oy=qy0+out[...,1]/H*(qy1-qy0)
            else: ox=qx0+(out[...,0]-bx0)/(bx1-bx0)*(qx1-qx0); oy=qy0+(out[...,1]-by0)/(by1-by0)*(qy1-qy0)
            srcx=S_x[pi]+u[None,:]*(S_x[pi+1]-S_x[pi]); srcy=S_y[pj]+v[:,None]*(S_y[pj+1]-S_y[pj])
            srcx=np.broadcast_to(srcx,ox.shape); srcy=np.broadcast_to(srcy,oy.shape)
            ix=np.floor(ox).astype(int); iy=np.floor(oy).astype(int)
            m=(ix>=0)&(ix<outW)&(iy>=0)&(iy<outH)
            uv[iy[m],ix[m],0]=srcx[m]; uv[iy[m],ix[m],1]=srcy[m]
    return uv
