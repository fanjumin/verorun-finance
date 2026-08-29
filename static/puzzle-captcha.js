/**
 * PuzzleCaptcha v5 — baked rotation, shape border, strong snap.
 */
(function() {
  if(typeof window.__==='undefined'){window.__=function(k){return k;};}
  const W=340,H=190,SNAP=55;
  let S={phase:'loading',bgImg:null,hole:null,pieces:[],activeIdx:null,
         dragOffX:0,dragOffY:0,token:'',trail:[],t0:0,API_BASE:''};
  let C,ctx,root;

  function build(){
    const el=document.getElementById('puzzle-captcha');
    if(!el||el._mounted)return;el._mounted=true;
    S.API_BASE=el.dataset.api||location.origin;
    el.innerHTML=`<div class="pc3-root"><div class="pc3-frame">
      <canvas class="pc3-canvas" width="${W}" height="${H}"></canvas>
      <div class="pc3-loading" id="pc3Load"><div class="pc3-skeleton"><div class="pc3-shimmer"></div></div><span>'+__('加载安全验证...')+'</span></div>
      <button class="pc3-refresh"><svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg></button>
    </div><div class="pc3-hint" id="pc3Hint">'+__('选择正确形状拖入缺口')+'</div></div>`;
    root=el.querySelector('.pc3-root');C=el.querySelector('.pc3-canvas');ctx=C.getContext('2d');
    C.addEventListener('mousedown',d);C.addEventListener('mousemove',m);C.addEventListener('mouseup',u);
    C.addEventListener('touchstart',td,{passive:false});C.addEventListener('touchmove',tm,{passive:false});C.addEventListener('touchend',tu);
    el.querySelector('.pc3-refresh').addEventListener('click',load);
  }

  function render(){
    if(!S.bgImg)return;
    ctx.clearRect(0,0,W,H);ctx.drawImage(S.bgImg,0,0,W,H);
    // Pieces
    S.pieces.forEach((p,i)=>{
      if(!p.img)return;
      // Draw piece image with drop shadow for depth
      ctx.save();
      ctx.shadowColor=S.activeIdx===i?'rgba(0,194,255,0.55)':'rgba(0,0,0,0.4)';
      ctx.shadowBlur=S.activeIdx===i?16:8;
      ctx.drawImage(p.img,p.x,p.y,p.w,p.h);
      ctx.restore();
    });
  }

  function hit(mx,my){
    for(let i=S.pieces.length-1;i>=0;i--){
      const p=S.pieces[i];
      if(mx>=p.x-2&&mx<=p.x+p.w+2&&my>=p.y-2&&my<=p.y+p.h+2)return i;
    }return-1;
  }

  function d(e){if(S.phase!=='ready')return;const r=C.getBoundingClientRect(),mx=(e.clientX-r.left)*(W/r.width),my=(e.clientY-r.top)*(H/r.height),idx=hit(mx,my);if(idx<0)return;const p=S.pieces[idx];S.activeIdx=idx;S.phase='dragging';S.dragOffX=mx-p.x;S.dragOffY=my-p.y;S.t0=Date.now();S.trail=[{t:0,x:p.x,y:p.y}];root.classList.add('pc3-dragging');e.preventDefault();}
  function td(e){if(S.phase!=='ready')return;const t=e.touches[0],r=C.getBoundingClientRect(),mx=(t.clientX-r.left)*(W/r.width),my=(t.clientY-r.top)*(H/r.height),idx=hit(mx,my);if(idx<0)return;const p=S.pieces[idx];S.activeIdx=idx;S.phase='dragging';S.dragOffX=mx-p.x;S.dragOffY=my-p.y;S.t0=Date.now();S.trail=[{t:0,x:p.x,y:p.y}];root.classList.add('pc3-dragging');e.preventDefault();}
  function m(e){if(S.phase!=='dragging')return;const r=C.getBoundingClientRect(),mx=(e.clientX-r.left)*(W/r.width),my=(e.clientY-r.top)*(H/r.height);const p=S.pieces[S.activeIdx];p.x=mx-S.dragOffX;p.y=my-S.dragOffY;if(S.hole){const h=S.hole,hcx=h.x+h.w/2,hcy=h.y+h.h/2,pcx=p.x+p.w/2,pcy=p.y+p.h/2;const dist=Math.sqrt((pcx-hcx)**2+(pcy-hcy)**2);if(dist<SNAP){const pull=Math.max(0,(SNAP-dist)/SNAP)*0.4;p.x+=(h.x-p.x)*pull;p.y+=(h.y-p.y)*pull;}}const now=Date.now();if(now-(S.trail[S.trail.length-1]?.t||0)>20)S.trail.push({t:now-S.t0,x:Math.round(p.x),y:Math.round(p.y)});render();}
  function tm(e){if(S.phase!=='dragging')return;e.preventDefault();const t=e.touches[0],r=C.getBoundingClientRect(),mx=(t.clientX-r.left)*(W/r.width),my=(t.clientY-r.top)*(H/r.height);const p=S.pieces[S.activeIdx];p.x=mx-S.dragOffX;p.y=my-S.dragOffY;if(S.hole){const h=S.hole,hcx=h.x+h.w/2,hcy=h.y+h.h/2,pcx=p.x+p.w/2,pcy=p.y+p.h/2;const dist=Math.sqrt((pcx-hcx)**2+(pcy-hcy)**2);if(dist<SNAP){const pull=Math.max(0,(SNAP-dist)/SNAP)*0.4;p.x+=(h.x-p.x)*pull;p.y+=(h.y-p.y)*pull;}}S.trail.push({t:Date.now()-S.t0,x:Math.round(p.x),y:Math.round(p.y)});render();}
  async function u(e){
    if(S.phase!=='dragging')return;root.classList.remove('pc3-dragging');
    const p=S.pieces[S.activeIdx],h=S.hole,hcx=h.x+h.w/2,hcy=h.y+h.h/2,pcx=p.x+p.w/2,pcy=p.y+p.h/2;
    const dist=Math.sqrt((pcx-hcx)**2+(pcy-hcy)**2);
    S.trail.push({t:Date.now()-S.t0,x:Math.round(p.x),y:Math.round(p.y)});
    if(dist<SNAP&&p.isTarget){p.x=h.x;p.y=h.y;S.activeIdx=null;S.phase='verifying';render();await verify();}
    else{p.x=p._ox;p.y=p._oy;S.activeIdx=null;S.phase='ready';render();root.classList.add('pc3-miss');setTimeout(()=>root.classList.remove('pc3-miss'),500);document.getElementById('pc3Hint').textContent=dist<SNAP?__('形状不匹配'):__('未对准');setTimeout(()=>{if(S.phase==='ready')document.getElementById('pc3Hint').textContent=__('选择正确形状拖入缺口');},1500);if(dist<SNAP)setTimeout(load,1200);}
  }
  function tu(){u();}

  async function load(){
    S.phase='loading';document.getElementById('pc3Load').style.display='flex';document.getElementById('pc3Hint').textContent=__('加载中...');root.classList.remove('pc3-success','pc3-miss');
    try{const res=await fetch(S.API_BASE+'/api/captcha/generate');const d=await res.json();
      S.token=d.token;S.hole=d.hole;S.pieces=d.pieces;S.bgImg=null;
      let total=S.pieces.length+1,loaded=0;
      function chk(){loaded++;if(loaded===total){S.phase='ready';document.getElementById('pc3Load').style.display='none';document.getElementById('pc3Hint').textContent=__('选择正确形状拖入缺口');render();}}
      S.pieces.forEach(p=>{p._ox=p.x;p._oy=p.y;p.img=new Image();p.img.onload=chk;p.img.onerror=chk;p.img.src=p.imgData;});
      const bg=new Image();bg.onload=()=>{S.bgImg=bg;chk();};bg.onerror=chk;bg.src='data:image/png;base64,'+d.background;
    }catch(e){document.getElementById('pc3Hint').textContent=__('加载失败');console.error(e);}
  }

  async function verify(){
    try{const r=await fetch(S.API_BASE+'/api/captcha/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:S.token,drag_distance:0,drag_trace:S.trail})});const d=await r.json();
      if(d.success){S.phase='success';root.classList.add('pc3-success');document.getElementById('pc3Hint').textContent=__('✓ 验证通过');
        document.getElementById('puzzle-captcha').dispatchEvent(new CustomEvent('captcha-success',{bubbles:true,detail:{token:S.token,risk_score:d.risk_score}}));}
      else{S.phase='ready';root.classList.add('pc3-miss');setTimeout(()=>root.classList.remove('pc3-miss'),500);document.getElementById('pc3Hint').textContent=__('验证失败');setTimeout(load,1200);}
    }catch(e){S.phase='ready';document.getElementById('pc3Hint').textContent=__('网络错误');}
  }

  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',()=>{build();load();}):(build(),load());
})();
