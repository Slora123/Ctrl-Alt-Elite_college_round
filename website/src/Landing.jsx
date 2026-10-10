import {useEffect,useRef,useState} from 'react'
import {Link} from 'react-router-dom'
import './landing.css'

// ---- content ----
const stages=[['RESEARCH PAPER','The claim and its text, as published.','M8 6h34v52H8zM16 18h18M16 28h18M16 38h12'],['METHODOLOGY','Steps, settings and gaps pulled from the text.','M6 52L22 34l12 8 18-26'],['DATASET','Which data, which split, and whether it is reachable.','M10 16h36M10 30h36M10 44h36M10 16v28M46 16v28'],['MODEL','Architecture and training parameters.','M8 32h10M46 32h10M18 14l28 18-28 18z'],['EVALUATION','Metrics and procedure used to score runs.','M10 52V30M26 52V16M42 52V38M6 54h48'],['PUBLISHED CLAIM','The number that can now be traced back.','M12 32l14 14 26-30']];
const caps=[['Research Understanding','Analyze research papers and supporting artifacts to extract methodologies, configurations, and experimental details.','M10 10h50v60H10zM20 25h30M20 38h30M20 51h18'],['Claim Verification','Help researchers assess whether published findings can be independently reproduced, while clearly exposing missing evidence and limitations.','M15 42l15 15 35-38'],['Experiment Reconstruction','Transform documented methodologies into structured, traceable experiment workflows.','M10 60V45h20V30h20V15h20'],['Version Comparison','Compare experiment versions, identify changed components, and visualize how those changes relate to differences in evaluation results.','M25 10v60M55 10v60M25 30h30M25 52h30'],['Research Auditing','Highlight methodological flaws, inconsistencies, missing information, and reproducibility risks.','M40 10L70 65H10zM40 30v18M40 56v2'],['Evidence-Based Reporting','Present findings, limitations, comparisons, and supporting evidence in transparent reports.','M12 65V35M32 65V20M52 65V45M8 67h56'],['Experiment Traceability','Maintain a clear history of experiment versions, configurations, evaluation results, and changes.','M15 40h20M35 40l20-22M35 40l20 22M15 40a4 4 0 100 .1']];

// ---- 3D desk scene (three.js is loaded on demand so dashboard-only visits never download it) ----
function initScene(THREE,cv,onFallback){
  const T=THREE,reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
  const R=new T.WebGLRenderer({canvas:cv,antialias:true});R.setPixelRatio(Math.min(devicePixelRatio,1.5));
  R.shadowMap.enabled=true;R.shadowMap.type=T.PCFSoftShadowMap;
  const S=new T.Scene();S.background=new T.Color(0xE6E0EE);S.fog=new T.Fog(0xE6E0EE,9,20);
  const cam=new T.PerspectiveCamera(36,1,.1,40);
  S.add(new T.HemisphereLight(0xfff3e6,0xb9b0d8,.75));
  const sun=new T.DirectionalLight(0xffe8cc,.95);sun.position.set(-4,6,3);sun.castShadow=true;sun.shadow.mapSize.set(1024,1024);
  Object.assign(sun.shadow.camera,{left:-5,right:5,top:5,bottom:-5});sun.shadow.radius=4;S.add(sun);
  const M=(c,r=.7)=>new T.MeshStandardMaterial({color:c,roughness:r});
  const beige=M(0xD9CFB6,.8),dark=M(0x3a372f,.6);
  function B(w,h,d,m,x,y,z){const o=new T.Mesh(new T.BoxGeometry(w,h,d),m);o.position.set(x,y,z);o.castShadow=o.receiveShadow=true;S.add(o);return o}
  // desk
  const desk=new T.Mesh(new T.PlaneGeometry(30,16),M(0xC9B79E,.9));desk.rotation.x=-Math.PI/2;desk.receiveShadow=true;S.add(desk);
  // CRT
  B(1.9,.16,1.5,beige,0,.08,0);B(1.5,.1,1.2,beige,0,.2,0);
  B(1.8,1.5,.5,beige,0,1.05,.35);B(1.35,1.1,.9,beige,0,1.02,-.4);
  const bez=B(1.5,1.2,.08,M(0xcbc1a8,.8),0,1.1,.62);
  function tex(w,h,f){const c=document.createElement('canvas');c.width=w;c.height=h;f(c.getContext('2d'),w,h);const t=new T.CanvasTexture(c);t.anisotropy=4;return t}
  const scr=tex(512,400,(g,w,h)=>{g.fillStyle='#1d1b2e';g.fillRect(0,0,w,h);g.font='24px monospace';g.fillStyle='#C8DDCB';
  ['> paper: demo-001','> claim: +2.8 [UNVERIFIED]','> warmup: MISSING','> split: UNDOCUMENTED','','> evidence required_','','ILLUSTRATIVE DEMO DATA'].forEach((l,i)=>{g.fillStyle=i==7?'#F4D6C2':i==2||i==3?'#F6B8A0':'#C8DDCB';g.fillText(l,28,60+i*38)});
  g.fillStyle='rgba(255,255,255,.04)';for(let y=0;y<h;y+=4)g.fillRect(0,y,w,1)});
  const sg=new T.PlaneGeometry(1.22,.95,12,10),p=sg.attributes.position;
  for(let i=0;i<p.count;i++){const x=p.getX(i),y=p.getY(i);p.setZ(i,.07*(1-(x*x)/.37-(y*y)/.23))}sg.computeVertexNormals();
  const sm=new T.Mesh(sg,new T.MeshBasicMaterial({map:scr,color:0xdddddd}));sm.position.set(0,1.13,.67);S.add(sm);
  B(.12,.03,.02,dark,.55,.6,.66);B(.12,.03,.02,dark,.7,.6,.66);
  for(let i=0;i<7;i++)B(.5,.012,.012,dark,0,.45+i*.012,-.2);
  // keyboard
  B(1.6,.08,.55,beige,0,.04,1.45).rotation.x=-.06;
  const keys=new T.InstancedMesh(new T.BoxGeometry(.09,.04,.09),M(0xe7dfca,.6),70),d=new T.Object3D();let k=0;
  for(let r=0;r<5;r++)for(let c=0;c<14;c++){d.position.set(-.67+c*.103,.11,1.28+r*.1);d.rotation.x=-.06;d.updateMatrix();keys.setMatrixAt(k++,d.matrix)}
  keys.castShadow=true;S.add(keys);
  // papers
  function paper(x,z,ry,kind){const t=tex(256,340,(g,w,h)=>{g.fillStyle='#FBF8F1';g.fillRect(0,0,w,h);g.fillStyle='#bdb7c8';for(let y=40;y<h-20;y+=9)g.fillRect(20,y,60+Math.random()*(w-80),2);
  if(kind){g.strokeStyle='#8170E8';g.lineWidth=3;g.beginPath();g.moveTo(24,170);for(let x=24;x<230;x+=20)g.lineTo(x,170-Math.pow(x-24,.8)*.9+Math.sin(x)*6);g.stroke();g.fillStyle='#fff';g.fillRect(14,150,230,6)}
  g.fillStyle='#5948B8';g.font='italic 22px serif';g.fillText(kind?'warmup?':'which split?',90,h-30)});
  const m=new T.Mesh(new T.BoxGeometry(1.3,.01,1.7),[M(0xeee8d8),M(0xeee8d8),new T.MeshStandardMaterial({map:t,roughness:.9}),M(0xeee8d8),M(0xeee8d8),M(0xeee8d8)]);
  m.position.set(x,.012+(kind?.012:0),z);m.rotation.y=ry;m.castShadow=m.receiveShadow=true;S.add(m)}
  paper(-2.1,1.3,.25,0);paper(-1.7,1.0,-.15,1);paper(1.9,1.4,-.3,0);
  // objects: mug, floppy
  const mug=new T.Mesh(new T.CylinderGeometry(.22,.2,.4,24),M(0xDCDDF8,.4));mug.position.set(2.2,.2,.1);mug.castShadow=true;S.add(mug);
  B(.5,.015,.5,M(0x5948B8,.5),1.3,.015,1.6).rotation.y=.4;
  // camera path
  const K=[[0,[1.6,2.2,4.9],[-1.5,.6,0]],[1,[-.3,3.6,5.0],[-1.25,.4,.4]],[2,[1.2,2.2,5.2],[0,1.1,0]],[3,[0,1.8,5.8],[0,1,0]],[4,[-.4,1.8,5.6],[0,1,0]],[5,[.9,1.6,5.2],[-1.2,.8,.3]]];
  const ids=['lp-top','lp-problem','lp-workflow','lp-caps','lp-lineage','lp-close'];
  const L=(a,b,t)=>a+(b-a)*t,ease=t=>.5*t+.5*t*t*(3-2*t),v=new T.Vector3();
  let cur=0,tgt=0,run=false,raf=0,dead=false;
  function pose(p){let i=0;while(i<K.length-2&&p>K[i+1][0])i++;const a=K[i],b=K[i+1],t=ease(Math.min(1,Math.max(0,(p-a[0])/(b[0]-a[0]))));
  cam.position.set(L(a[1][0],b[1][0],t),L(a[1][1],b[1][1],t),L(a[1][2],b[1][2],t));
  const mob=innerWidth<860;if(mob)cam.position.z+=1.2;
  cam.lookAt(L(a[2][0],b[2][0],t)*(mob?.2:1),L(a[2][1],b[2][1],t)+(mob?.9:0),L(a[2][2],b[2][2],t))}
  function size(){if(dead)return;R.setSize(innerWidth,innerHeight,false);cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();pose(cur);R.render(S,cam)}
  function frame(){if(dead)return;const d=tgt-cur;cur=reduce||Math.abs(d)<.0004?tgt:cur+d*.09;pose(cur);R.render(S,cam);if(cur!==tgt)raf=requestAnimationFrame(frame);else run=false}
  function kick(){if(dead)return;const tp=ids.map(id=>document.getElementById(id).getBoundingClientRect().top+scrollY),y=scrollY;let i=0;while(i<tp.length-2&&y>=tp[i+1])i++;const t=Math.min(1,Math.max(0,(y-tp[i])/(tp[i+1]-tp[i]))),u=Math.min(1,t/.8);tgt=i+u;if(!run){run=true;raf=requestAnimationFrame(frame)}}
  const onResize=()=>{size();kick()};addEventListener('scroll',kick,{passive:true});addEventListener('resize',onResize);
  const onLost=e=>{e.preventDefault();onFallback()};cv.addEventListener('webglcontextlost',onLost);
  size();kick();
  return()=>{dead=true;cancelAnimationFrame(raf);removeEventListener('scroll',kick);removeEventListener('resize',onResize);cv.removeEventListener('webglcontextlost',onLost);
  S.traverse(o=>{if(o.geometry)o.geometry.dispose();const ms=o.material?(Array.isArray(o.material)?o.material:[o.material]):[];ms.forEach(m=>{if(m.map)m.map.dispose();m.dispose()})});
  R.dispose();R.forceContextLoss()}
}

const DASH='/mission' // dashboard home route

export default function Landing(){
  const glRef=useRef(null)
  const [nogl,setNogl]=useState(false)

  useEffect(()=>{window.scrollTo(0,0)},[])

  useEffect(()=>{
    const host=glRef.current
    if(!host)return
    let stop=null,gone=false
    const cv=document.createElement('canvas')
    cv.setAttribute('aria-hidden','true')
    host.appendChild(cv)
    let webgl=false
    try{webgl=!!(cv.getContext('webgl')||cv.getContext('experimental-webgl'))}catch(e){}
    if(!webgl){setNogl(true);return()=>{cv.remove()}}
    import('three').then(THREE=>{
      if(gone)return
      try{stop=initScene(THREE,cv,()=>setNogl(true))}catch(e){setNogl(true)}
    }).catch(()=>{if(!gone)setNogl(true)})
    return()=>{gone=true;if(stop)stop();cv.remove()}
  },[])

  const jump=id=>()=>{
    const el=document.getElementById(id)
    if(el)el.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'})
  }

  return(
    <div className={'lp-root'+(nogl?' lp-nogl':'')}>
      <div className="lp-gl" ref={glRef} aria-hidden="true"/>
      <div className="lp-grain" aria-hidden="true"/>
      <Link className="lp-skip" to={DASH}>SKIP INTRO</Link>
      <div className="lp-main">
        <section className="lp-hero" id="lp-top"><div className="lp-wrap"><div className="lp-box">
          <h1>CRISISGRID</h1>
          <p className="lp-q">Can this AI research actually be reproduced?</p>
          <p>Published results are claims. Reproducibility requires evidence.</p>
          <div className="lp-mono">PAPERS · EXPERIMENTS · CLAIMS · EVIDENCE</div>
          <button type="button" className="lp-cue lp-mono" onClick={jump('lp-problem')}>SCROLL TO INVESTIGATE ↓</button>
        </div></div></section>

        <section className="lp-s lp-prob" id="lp-problem"><div className="lp-wrap"><div>
          <div className="lp-panel"><h2>Reading a paper is not reproducing it.</h2>
            <p>A reported gain is only a starting point. To check it, you need the exact setup behind the number, and that setup is rarely all on the page.</p>
            <ul className="lp-issues"><li>Missing experimental configurations</li><li>Incomplete methodology documentation</li><li>Inaccessible or undocumented datasets</li><li>Inconsistent evaluation procedures</li><li>Results that are difficult to independently reproduce</li></ul></div>
        </div></div></section>

        <section className="lp-s lp-solid" id="lp-workflow"><div className="lp-wrap">
          <div className="lp-sec-h"><h2>From a paper to a traceable experiment.</h2><p>The planned CrisisGrid workflow follows a claim back through each stage that produced it, and shows where the record has gaps.</p></div>
          <div className="lp-pipe">
            {stages.map((s,i)=>(
              <div className="lp-node" key={s[0]}>
                <svg viewBox="0 0 64 64" fill="none" stroke={i%2?'#5948B8':'#8170E8'} strokeWidth="2" aria-hidden="true"><path d={s[2]}/></svg>
                <b>{s[0]}</b><span>{s[1]}</span>
              </div>
            ))}
          </div>
        </div></section>

        <section className="lp-s lp-solid lp-capsec" id="lp-caps"><div className="lp-wrap"><div className="lp-capgrid">
          <div className="lp-caphead"><h2>Seven planned capabilities.</h2><p>None of these are built yet. This is what we intend to make possible.</p></div>
          {caps.map((c,i)=>(
            <div className={'lp-cap lp-t'+i} key={c[0]}>
              <div className="lp-top"><div className="lp-num">0{i+1}</div>
                <div className="lp-ic"><svg viewBox="0 0 80 80" fill="none" stroke={i%2?'#8170E8':'#5948B8'} strokeWidth="2" aria-hidden="true"><path d={c[2]}/></svg></div></div>
              <h3>{c[0]}</h3><p>{c[1]}</p>
            </div>
          ))}
        </div></div></section>

        <section className="lp-s lp-solid" id="lp-lineage" style={{paddingTop:'2vh'}}><div className="lp-wrap">
          <div className="lp-sec-h"><span className="lp-tag">ILLUSTRATIVE DEMONSTRATION</span><h2 style={{marginTop:14}}>Every change, on the record.</h2><p>A hypothetical original experiment and two modified versions. Each branch records what changed, which configuration ran, and how evaluation differed.</p></div>
          <div className="lp-lin">
            <svg viewBox="0 0 560 330" role="img" aria-label="Experiment lineage graph: v0 original, v1 changes dataset split, v2 changes evaluation metric">
              <g stroke="#8170E8" strokeWidth="2" fill="none"><path d="M120 165H230C260 165 260 80 300 80H430"/><path d="M230 165C260 165 260 250 300 250H430"/><path d="M120 165H230"/></g>
              <g fontFamily="IBM Plex Mono,monospace" fontSize="11" fill="#27263B">
                <rect x="30" y="140" width="110" height="50" rx="3" fill="#DCDDF8" stroke="#8170E8"/><text x="85" y="162" textAnchor="middle" fontWeight="500">v0 original</text><text x="85" y="178" textAnchor="middle" fill="#77758D">acc 91.2</text>
                <rect x="300" y="52" width="150" height="56" rx="3" fill="#C8DDCB" stroke="#5948B8"/><text x="375" y="75" textAnchor="middle" fontWeight="500">v1 dataset split</text><text x="375" y="92" textAnchor="middle" fill="#77758D">acc 89.7 (demo)</text>
                <rect x="300" y="222" width="150" height="56" rx="3" fill="#F4D6C2" stroke="#5948B8"/><text x="375" y="245" textAnchor="middle" fontWeight="500">v2 eval metric</text><text x="375" y="262" textAnchor="middle" fill="#77758D">F1 88.9 (demo)</text>
                <text x="205" y="150" fill="#5948B8">fork</text><text x="30" y="318" fill="#77758D">ILLUSTRATIVE DEMO DATA</text>
              </g>
            </svg>
            <div>
              <table className="lp-cmp"><tbody>
                <tr><th></th><th>v0</th><th>v1</th><th>v2</th></tr>
                <tr><td>Changed</td><td>—</td><td>train/test split</td><td>metric</td></tr>
                <tr><td>Config</td><td>cfg-a</td><td>cfg-a</td><td>cfg-a</td></tr>
                <tr><td>Eval</td><td>top-1</td><td>top-1</td><td>macro-F1</td></tr>
                <tr><td>Score</td><td>91.2</td><td>89.7</td><td>88.9</td></tr>
                <tr><td>Evidence</td><td>paper only</td><td>run log</td><td>run log</td></tr>
              </tbody></table>
              <p className="lp-fine">Scores are invented. A difference between versions shows that something changed, not why. Establishing cause would take controlled runs.</p>
            </div>
          </div>
        </div></section>

        <section className="lp-end" id="lp-close"><div className="lp-wrap">
          <div><h2>Make every research claim traceable.</h2><p>Move from published claims toward transparent, reproducible evidence.</p><Link className="lp-cta" to={DASH}>EXPLORE THE VISION</Link></div>
        </div></section>
      </div>
    </div>
  )
}
