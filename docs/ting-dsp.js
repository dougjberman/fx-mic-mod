/* ting-dsp.js: browser approximation of the fx-mic-mod firmware, for trying presets
 * on a computer mic. Taken unchanged from this project's TING Effects Bench preview
 * (dspFactory). Autotune follows firmware/autotune.py; echo, multi-tap delay, chorus and
 * reverb follow CircuitPython's audiodelays and audiofreeverb source step for step; the
 * other effects approximate audiofilters, so they sound close to the mic but not identical.
 *
 * SPDX-License-Identifier: MIT
 */
"use strict";
/* =====================================================================
   DSP library. Runs in the AudioWorklet (and on the main thread for
   validation). Autotune is ported from firmware/code.py and follows
   fork/CONFIG_FORMAT.md; the other effects approximate CircuitPython's
   audiodelays / audiofilters / audiofreeverb. Parameter defaults marked
   ASSUMED are not checked against the CircuitPython constructors.
   ===================================================================== */
function dspFactory(){
  const BASE=16000;
  const log2=x=>Math.log(x)/Math.LN2;
  const SCALES={chromatic:[0,1,2,3,4,5,6,7,8,9,10,11], major:[0,2,4,5,7,9,11], minor:[0,2,3,5,7,8,10]};
  const NOTE_IDX={C:0,"C#":1,DB:1,D:2,"D#":3,EB:3,E:4,F:5,"F#":6,GB:6,G:7,"G#":8,AB:8,A:9,"A#":10,BB:10,B:11};
  const norm=s=>String(s).replace(/([a-z0-9])([A-Z])/g,"$1_$2").toLowerCase().replace(/[ -]/g,"_");
  const clamp=(v,a,b)=>v<a?a:v>b?b:v;
  // CircuitPython's synthio_mix_down_sample: a soft knee above 28000/32768, then int16 saturation.
  // mixScale(n) is SYNTHIO_MIX_DOWN_SCALE(n) as a slope; MIX_HARD is the near-hard limit Chorus uses
  const KNEE=28000/32768, mixScale=n=>Math.floor(0xfffffff/(32768*n-28000))/65536, MIX_2=mixScale(2), MIX_HARD=2/65536;
  const sat=v=>v>1?1:v<-1?-1:v;
  const mixDown=(v,sc)=>sat(v>KNEE?(v-KNEE)*sc+KNEE:v<-KNEE?(v+KNEE)*sc-KNEE:v);

  /* ---------- harmonizer chords, as firmware/autotune.py (CHORDS, KEY_CHORDS, voicing) ---------- */
  const CHORDS={octave:[0,12], maj3:[0,4], min3:[0,3], fifth:[0,7], power:[0,7,12], major:[0,4,7], minor:[0,3,7], dim:[0,3,6], aug:[0,4,8], sus2:[0,2,7], sus4:[0,5,7],
    "6":[0,4,7,9], m6:[0,3,7,9], "6/9":[0,4,9,14], maj7:[0,4,7,11], "7":[0,4,7,10], m7:[0,3,7,10], m_maj7:[0,3,7,11], m7b5:[0,3,6,10],
    dim7:[0,3,6,9], "7sus4":[0,5,7,10], aug7:[0,4,8,10], aug_maj7:[0,4,8,11], add9:[0,4,7,14], m_add9:[0,3,7,14], add11:[0,4,7,17],
    maj9:[0,4,11,14], "9":[0,4,10,14], m9:[0,3,10,14], "7b9":[0,4,10,13], "7#9":[0,4,10,15], "maj7#11":[0,4,11,18],
    "11":[0,10,14,17], m11:[0,3,10,17], "13":[0,4,10,21], maj13:[0,4,11,21], m13:[0,3,10,21], quartal:[0,5,10,15]};
  const KEY_CHORDS={key_third:[0,2], key_triad:[0,2,4], key_seventh:[0,2,4,6]};
  const OFF_KEY_SHAPE=[0,4,7,10];
  const maxInversion=t=>t.slice(1).some(x=>x%12===0) ? 0 : t.length-1;
  function voicing(tones, inversion, octave){
    let t=tones.slice();
    for(let k=0;k<inversion;k++) t.push(t.shift()+12);
    if(inversion) t=t.map(x=>x-12);
    t.splice(t.indexOf(0),1);
    return t.map(x=>x+12*(octave||0));
  }
  // semitones of the chord built in scale steps on the snapped note, or null off the scale (firmware key_tones)
  function keyTones(note, snap, steps){
    const allowed=snap.allowed.length===12 ? SCALES.major : snap.allowed;
    const pcs=allowed.map(pc=>(pc+snap.root)%12).sort((a,b)=>a-b), pc=((note%12)+12)%12, i=pcs.indexOf(pc), n=pcs.length;
    return i<0 ? null : steps.map(st=>pcs[(i+st)%n]-pc+12*Math.floor((i+st)/n));
  }
  function keyIntervals(note, snap, steps, inversion, octave){
    return voicing(keyTones(note, snap, steps)||OFF_KEY_SHAPE.slice(0,steps.length), inversion, octave);
  }

  /* ---------- blocks: numbers, {"block": ...}, "$name" ---------- */
  const SHAPES={sine:t=>Math.sin(2*Math.PI*t), triangle:t=>t<0.25?4*t:(t<0.75?2-4*t:4*t-4), square:t=>t<0.5?1:-1, saw:t=>2*t-1, ramp_down:t=>1-2*t};
  const WALIAS={ramp_up:"saw",sawtooth:"saw",ramp:"saw",reverse_saw:"ramp_down"};
  function waveFn(w,name){
    if(w==null) return SHAPES.triangle;   // synthio's built-in triangle
    if(typeof w==="string"){ let k=norm(w); k=WALIAS[k]||k; if(!SHAPES[k]) throw new Error("unknown waveform shape: "+w); return SHAPES[k]; }
    if(Array.isArray(w)){ if(w.length<2) throw new Error(name+" needs at least 2 samples"); const a=w.map(v=>v/32767), n=a.length;
      return t=>{ const p=t*n, i=Math.floor(p)%n, f=p-Math.floor(p); return a[i]+(a[(i+1)%n]-a[i])*f; }; }
    if(typeof w==="object"){ if(!("shape" in w)) throw new Error(name+" requires a shape"); return waveFn(w.shape,name); }
    throw new Error(name+" must be a name, dict or list");
  }
  const MATH={ sum:(a,b,c)=>a+b+c, add_sub:(a,b,c)=>a+b-c, product:(a,b,c)=>a*b*c, mul_div:(a,b,c)=>c?a*b/c:0,
    scale_offset:(a,b,c)=>a*b+c, offset_scale:(a,b,c)=>(a+b)*c, lerp:(a,b,c)=>a*(1-c)+b*c,
    constrained_lerp:(a,b,c)=>{ c=clamp(c,0,1); return a*(1-c)+b*c; }, div_add:(a,b,c)=>b?a/b+c:c, add_div:(a,b,c)=>c?(a+b)/c:0,
    mid:(a,b,c)=>Math.max(Math.min(a,b),Math.min(Math.max(a,b),c)), max:(a,b,c)=>Math.max(a,b,c), min:(a,b,c)=>Math.min(a,b,c), abs:a=>Math.abs(a) };
  class NS {
    constructor(defs,host){ if(defs!=null&&(typeof defs!=="object"||Array.isArray(defs))) throw new Error('"blocks" must be a mapping of name to block');
      this.defs=defs||{}; this.host=host||{}; this.built={}; this.resolving=[]; }
    ref(r,name){
      const k=r.slice(1); if(!k) throw new Error(name+" has an empty block reference");
      if(k in this.host) return this.host[k];
      if(this.built[k]) return this.built[k];
      if(!(k in this.defs)) throw new Error(name+" refers to undefined block $"+k);
      if(this.resolving.includes(k)) throw new Error("block $"+k+" depends on itself");
      this.resolving.push(k);
      try{ this.built[k]=compile(this.defs[k],"block $"+k,this); } finally{ this.resolving.pop(); }
      return this.built[k];
    }
  }
  function compile(v,name,ns){
    if(typeof v==="number"){ return ()=>v; }
    if(typeof v==="boolean"){ const n=v?1:0; return ()=>n; }
    if(typeof v==="string"){ if(v[0]!=="$") throw new Error(name+' must be a number, a block, or a "$name" reference'); return ns.ref(v,name); }
    if(v&&typeof v==="object"&&!Array.isArray(v)){
      const kind=v.block!=null?v.block:v.type;
      if(kind==null) throw new Error(name+' block is missing the "block" key');
      const key=norm(kind);
      if(key==="lfo"){
        for(const p in v) if(!["block","type","waveform","rate","scale","offset","phase_offset","once","interpolate"].includes(p)) throw new Error("LFO has no parameter "+p);
        const rate=compile(v.rate??1,name+" rate",ns), scale=compile(v.scale??1,name+" scale",ns), offset=compile(v.offset??0,name+" offset",ns),
          po=compile(v.phase_offset??0,name+" phase_offset",ns), wf=waveFn(v.waveform,name+" waveform"), once=!!v.once;
        let phase=0, frame=-1, val=0, done=false;
        return env=>{
          if(env.frame!==frame){
            if(frame>=0&&!done){ phase+=rate(env)*env.dt; if(once&&phase>=1){ phase=1; done=true; } else phase-=Math.floor(phase); }
            frame=env.frame;
            let t=phase+po(env); t-=Math.floor(t); if(done) t=0.99999;
            val=offset(env)+scale(env)*wf(t);
          }
          return val;
        };
      }
      if(key==="math"){
        for(const p in v) if(!["block","type","operation","a","b","c"].includes(p)) throw new Error("Math has no parameter "+p);
        if(!("operation" in v)) throw new Error("Math requires operation"); if(!("a" in v)) throw new Error("Math requires a");
        const op=MATH[norm(v.operation)]; if(!op) throw new Error("unknown math operation: "+v.operation);
        const a=compile(v.a,name+" a",ns), b=compile(v.b??0,name+" b",ns), c=compile(v.c??1,name+" c",ns);
        return env=>op(a(env),b(env),c(env));
      }
      throw new Error("unknown block type: "+kind);
    }
    throw new Error(name+" must be a number, block or block reference");
  }
  const num=(v,name,d)=>{ if(v==null) return d; if(typeof v!=="number") throw new Error(name+" must be a number"); return v; };

  /* ---------- pitch detection, ported from code.py ---------- */
  class Detector {
    constructor(sr,g){ this.sr=sr; this.g=g; const S=sr/BASE; this.BLOCK=Math.round(256*S); this.BUF=2*this.BLOCK; this.DEC=Math.max(2,2*Math.round(S));
      this.win=new Float32Array(this.BUF); this.blk=new Float32Array(this.BLOCK); this.pos=0; }
    push(x){ this.blk[this.pos++]=x; if(this.pos>=this.BLOCK){ this.pos=0; this.win.copyWithin(0,this.BLOCK); this.win.set(this.blk,this.BLOCK); return true; } return false; }
    detect(){ return this.g.detector==="original" ? this.original(this.win) : this.yin(this.win); }
    yin(buf){
      const p=this.g, sr=this.sr, N=buf.length, DEC=this.DEC;
      let mean=0; for(let i=0;i<N;i++) mean+=buf[i]; mean/=N;
      const x=new Float32Array(N); let e=0;
      for(let i=0;i<N;i++){ x[i]=(buf[i]-mean)*32768; e+=x[i]*x[i]; }
      if(Math.sqrt(e/N)<p.minRms) return 0;
      const ND=Math.floor(N/DEC), xd=new Float32Array(ND);
      for(let i=0;i<ND;i++){ let s=0; for(let k=0;k<DEC;k++) s+=x[i*DEC+k]; xd[i]=s/DEC; }
      const minLag=Math.floor(sr/p.maxHz), maxLag=Math.floor(sr/p.minHz);
      const maxLagD=Math.floor(maxLag/DEC)+1, minLagD=Math.max(2,Math.floor(minLag/DEC));
      const winD=ND-maxLagD, winF=N-maxLag-3;
      if(winD<8||winF<8) return 0;
      const diff=(a,lag,w)=>{ let t=0; for(let i=0;i<w;i++){ const d=a[i]-a[i+lag]; t+=d*d; } return t; };
      let running=0, bestLag=0, bestVal=2, prev=2, found=false;
      for(let lag=1;lag<maxLagD;lag++){
        const d=diff(xd,lag,winD); running+=d;
        const nrm= running>0 ? d*lag/running : 1;
        if(lag<minLagD){ prev=nrm; continue; }
        if(nrm<p.vt){ found=true; if(nrm<bestVal){ bestVal=nrm; bestLag=lag; } else if(nrm>prev) break; }
        else if(found) break;
        else if(nrm<bestVal){ bestVal=nrm; bestLag=lag; }
        prev=nrm;
      }
      if(bestLag===0||(!found&&bestVal>0.4)) return 0;
      const centre=bestLag*DEC, lo=Math.max(minLag,centre-DEC-1), hi=Math.min(maxLag+1,centre+DEC+2);
      const vals=[]; for(let lag=lo;lag<hi;lag++) vals.push(diff(x,lag,winF));
      let i=0; for(let k=1;k<vals.length;k++) if(vals[k]<vals[i]) i=k;
      let period=lo+i;
      if(i>0&&i<vals.length-1){ const a=vals[i-1],b=vals[i],c=vals[i+1],den=a-2*b+c; if(den>0) period+=0.5*(a-c)/den; }
      return sr/period;
    }
    original(buf){
      const p=this.g, sr=this.sr, N=buf.length;
      let e=0; for(let i=0;i<N;i++) e+=buf[i]*buf[i];
      if(Math.sqrt(e/N)*32768<p.minRms) return 0;
      const minP=Math.floor(sr/p.maxHz), maxP=Math.floor(sr/p.minHz);
      let best=-1, bestC=-1;
      for(let off=minP;off<maxP;off++){ let c=0; for(let i=0;i<N-off;i++) c+=Math.abs(buf[i]-buf[i+off]); if(bestC===-1||c<bestC){ bestC=c; best=off; } }
      return best>0 ? sr/best : 0;
    }
  }
  class NoteSnapper {
    constructor(scale,root,g){ this.allowed=SCALES[scale]; this.root=root; this.target=null; this.leap=null; this.g=g; }
    nearest(m){ let best=null;
      for(let o=Math.floor(m/12)-1;o<=Math.floor(m/12)+1;o++) for(const pc of this.allowed){ const n=o*12+(pc+this.root)%12; if(best===null||Math.abs(n-m)<Math.abs(best-m)) best=n; }
      return best; }
    correction(hz){
      if(hz<50||hz>1000){ this.target=null; return null; }
      const m=12*log2(hz/440)+69, near=this.nearest(m), h=this.g.hyst;
      if(this.target===null||near===this.target) this.target=near;
      else if(Math.abs(m-this.target)>Math.abs(m-near)+2*h) this.target=near;
      else if(Math.abs(m-this.target)>1.5){
        // jumped well away: follow once heard twice in a row, so a one-off octave error doesn't swap the chord
        if(this.leap===near){ this.target=near; this.leap=null; } else this.leap=near;
        return this.target-m;
      }
      this.leap=null;
      return this.target-m;
    }
  }

  /* ---------- grain shifters ---------- */
  // audiodelays.GranularPitchShift (CircuitPython 10.3, shared-module/audiodelays/GranularPitchShift.c), step for step:
  // a 2-grain capture line, Hann grains launched every grain/density samples starting one grain behind
  // the write point (plus up to spread*grain of backward jitter), each reading at the rate it was launched
  // with, summed and scaled by 2/density (at most 1). tick() returns the wet signal.
  class CPGranular {
    constructor(grain, density, spread){
      const g=Math.max(2,Math.round(grain)); this.g=g; this.L=2*g; this.cap=new Float32Array(this.L); this.w=0;
      this.d=clamp(Math.round(density),1,8); this.spread=clamp(spread||0,0,1); this.gain=Math.min(1,2/this.d);
      this.spacing=Math.max(1,Math.floor(g/this.d)); this.next=0; this.rng=0x1234abcd;
      this.env=new Float32Array(g); const den=g>1?g-1:1; for(let n=0;n<g;n++) this.env[n]=Math.trunc(0.5*(1-Math.cos(2*Math.PI*n/den))*32767)/32768;
      this.gr=[]; for(let k=0;k<8;k++) this.gr.push({on:false,ri:0,rate:1,ph:0});
    }
    rand(){ let x=this.rng; x^=x<<13; x>>>=0; x^=x>>>17; x^=x<<5; x>>>=0; this.rng=x; return x; }
    tick(x,ratio){
      const L=this.L, cap=this.cap; cap[this.w]=x; let s=0;
      for(const q of this.gr){ if(!q.on) continue; const ip=Math.floor(q.ri), f=q.ri-ip, lo=cap[ip%L], hi=cap[(ip+1)%L]; s+=(lo+(hi-lo)*f)*this.env[q.ph]; }
      s*=this.gain;
      for(const q of this.gr){ if(!q.on) continue; q.ri+=q.rate; q.ph++; if(q.ph>=this.g) q.on=false; }
      if(this.next===0){
        const q=this.gr.find(q=>!q.on);
        if(q){ let j=0; const mj=Math.floor(this.spread*this.g); if(mj>0) j=this.rand()%(mj+1);
          q.on=true; q.ri=(this.w+L-this.g-j)%L; q.rate=ratio; q.ph=0; }
        this.next=this.spacing;
      } else this.next--;
      this.w=(this.w+1)%L;
      return s;
    }
  }
  // audiodelays.PitchShift, step for step: window and overlap are in bytes (16-bit, so samples = bytes/2).
  class CPPitch {
    constructor(windowBytes, overlapBytes){
      this.ws=Math.max(1,Math.floor(windowBytes/2)); this.os=Math.max(0,Math.floor(overlapBytes/2));
      this.win=new Float32Array(this.ws); this.ov=this.os?new Float32Array(this.os):null; this.wi=0; this.oi=0; this.ri=0;
    }
    tick(x,ratio){
      const ws=this.ws, os=this.os, win=this.win, ov=this.ov;
      if(os){ win[this.wi]=ov[this.oi]; ov[this.oi]=x; } else win[this.wi]=x;
      const r=Math.floor(this.ri), off=r+ws*(r<this.wi?1:0)-this.wi;
      let y=win[r];
      if(os&&off>0&&off<=os) y=(y*off+ov[(this.oi+off)%os]*(os-off))/os;
      this.wi=(this.wi+1)%ws; if(os) this.oi=(this.oi+1)%os;
      this.ri+=ratio; if(this.ri>=ws) this.ri-=ws;
      return y;
    }
  }
  // PitchShift and GranularPitchShift's dry/wet law: mix doubled, each side capped at 1, then mix_down.
  const shiftMix=(x,y,m)=>mixDown(x*Math.min(2-2*m,1)+y*Math.min(2*m,1),MIX_2);
  class LockedShifter {         // OLAPitchShifter from code.py: grain held at a whole number of pitch periods
    constructor(grain){ this.grain=grain; this.target=grain; this.baseHalf=Math.floor(grain/2); let N=1024; while(N<grain*4) N*=2;
      this.N=N; this.hist=new Float32Array(N); this.w=0; this.p=0.25; this.dg=0; }
    lock(period){ if(period>0){ const k=Math.max(1,Math.round(this.baseHalf/period)); this.target=2*k*period; } }
    begin(n){ this.dg=(this.target-this.grain)*0.25/n; }
    tick(x,ratio){
      const N=this.N, h=this.hist, g=this.grain; h[this.w]=x;
      let p=this.p, pb=p+0.5; if(pb>=1) pb-=1;
      let r=this.w-p*g-2+N; let i0=Math.floor(r); let s0=h[i0%N]; const va=s0+(h[(i0+1)%N]-s0)*(r-i0);
      r=this.w-pb*g-2+N; i0=Math.floor(r); s0=h[i0%N]; const vb=s0+(h[(i0+1)%N]-s0)*(r-i0);
      const sa=Math.sin(Math.PI*p), sb=Math.sin(Math.PI*pb);
      this.w=(this.w+1)%N; this.grain=g+this.dg;
      p+=(1-ratio)/g; if(p>=1) p-=1; else if(p<0) p+=1; this.p=p;
      return va*sa*sa+vb*sb*sb;
    }
  }

  /* ---------- vocoder, as firmware/autotune.py (Vocoder) ----------
     A carrier at the sung (or tuned) note through one band-pass per band; the band levels come from
     an FFT of the newest power-of-two samples of the pitch window, every other block like the mic. */
  const VOC={WAVEFORMS:["saw","square","pulse","saw_square"], MAX_BANDS:11, LOW:180, HIGH:5000, BREATH:4000, HARMONICS:48, TABLE:256, NOISE:2048, GAIN:0.7, RELEASE:0.4, GATE:-50, GATE_OFF:-80, GATE_CLOSE:6, GATE_HOLD:4};
  const vocWaves={};
  function carrierWave(shape){
    if(!VOC.WAVEFORMS.includes(shape)) throw new Error("vocoder waveform must be saw, square, pulse or saw_square");
    if(vocWaves[shape]) return vocWaves[shape];
    const n=VOC.TABLE, terms=[];
    for(let k=1;k<=VOC.HARMONICS;k++) terms.push(shape==="saw"?[k,1/k,false]: shape==="square"?[k,k%2?1/k:0,false] : shape==="saw_square"?[k,k%2?2/k:1/k,false] : [k,Math.sin(Math.PI*k*0.25)/k,true]);
    const w=new Float64Array(n);
    for(const [k,a,cos] of terms) if(a) for(let i=0;i<n;i++){ const t=2*Math.PI*k*i/n; w[i]+=a*(cos?Math.cos(t):Math.sin(t)); }
    let pk=0; for(let i=0;i<n;i++) pk=Math.max(pk,Math.abs(w[i]));
    const scale=30000/pk, table=new Float32Array(n);
    for(let i=0;i<n;i++) table[i]=Math.trunc(w[i]*scale)/32768;
    let density=0; for(const [k,a] of terms) density+=(k*a*scale)**2; density/=terms.length;
    return vocWaves[shape]={table, density};
  }
  // carrier chords in just intonation, as whole multiples of a sub-note (firmware VOCODER_CHORDS)
  const VOC_CHORDS={major:[4,5,6], minor:[10,12,15], "7":[4,5,6,7], maj7:[8,10,12,15], m7:[10,12,15,18], power:[2,3,4], dim:[5,6,7], m7b5:[5,6,7,9]};
  // chords from the autotune key (firmware VOCODER_KEY_CHORDS, VOCODER_SHAPES, vocoder_key_chord)
  const VOC_KEY_CHORDS={key_triad:[0,2,4], key_seventh:[0,2,4,6]};
  const VOC_SHAPES={"0,4,7":"major","0,3,7":"minor","0,3,6":"dim","0,4,7,11":"maj7","0,3,7,10":"m7","0,4,7,10":"7","0,3,6,10":"m7b5"};
  const vocKeyChord=(tones, steps)=>(tones&&VOC_SHAPES[tones.join(",")])||(steps.length>3?"7":"major");
  function chordWave(shape, chord){
    const key=shape+"/"+chord; if(vocWaves[key]) return vocWaves[key];
    const r=VOC_CHORDS[chord]; if(!r) throw new Error("vocoder chord must be one of "+Object.keys(VOC_CHORDS).concat(Object.keys(VOC_KEY_CHORDS)).join(", "));
    const {table:base, density}=carrierWave(shape), size=base.length, n=128*r[r.length-1], w=new Float64Array(n);
    for(const m of r){ const step=m*size/n; for(let i=0;i<n;i++){ const p=i*step, j=Math.floor(p), a=base[j%size]; w[i]+=a+(base[(j+1)%size]-a)*(p-j); } }
    let pk=0; for(let i=0;i<n;i++) pk=Math.max(pk,Math.abs(w[i]));
    const scale=(30000/32768)/pk, table=new Float32Array(n); for(let i=0;i<n;i++) table[i]=Math.trunc(w[i]*scale*32768)/32768;
    // the base table is in -1..1 here; in int16 units the rescale is scale*32768 over the base's 32768
    return vocWaves[key]={table, root:r[0], density:density*scale*scale*r.reduce((a,b)=>a+b,0)/r[0]};
  }
  // power spectrum |FFT(x)|^2 of a power-of-two length, first half
  function powerSpectrum(x){
    const n=x.length, re=Float64Array.from(x), im=new Float64Array(n);
    for(let i=1,j=0;i<n;i++){ let b=n>>1; for(;j&b;b>>=1) j^=b; j|=b; if(i<j){ const t=re[i]; re[i]=re[j]; re[j]=t; } }
    for(let len=2;len<=n;len<<=1){ const a=-2*Math.PI/len, wr=Math.cos(a), wi=Math.sin(a);
      for(let i=0;i<n;i+=len){ let cr=1, ci=0;
        for(let k=0;k<len/2;k++){ const u=i+k, v=u+len/2, tr=re[v]*cr-im[v]*ci, ti=re[v]*ci+im[v]*cr;
          re[v]=re[u]-tr; im[v]=im[u]-ti; re[u]+=tr; im[u]+=ti; const nr=cr*wr-ci*wi; ci=cr*wi+ci*wr; cr=nr; } } }
    const p=new Float64Array(n/2); for(let i=0;i<n/2;i++) p[i]=re[i]*re[i]+im[i]*im[i]; return p;
  }
  const bandPass=(f,Q,sr)=>{ const w=2*Math.PI*f/sr, cs=Math.cos(w), al=Math.sin(w)/(2*Q), a0=1+al; return {b0:al/a0, b2:-al/a0, a1:-2*cs/a0, a2:(1-al)/a0, x1:0,x2:0,y1:0,y2:0}; };
  const highPass=(f,Q,sr)=>{ const w=2*Math.PI*f/sr, cs=Math.cos(w), al=Math.sin(w)/(2*Q), a0=1+al; return {b0:(1+cs)/2/a0, b1:-(1+cs)/a0, b2:(1+cs)/2/a0, a1:-2*cs/a0, a2:(1-al)/a0, x1:0,x2:0,y1:0,y2:0}; };
  const biq=(f,x)=>{ const y=f.b0*x+(f.b1||0)*f.x1+f.b2*f.x2-f.a1*f.y1-f.a2*f.y2; f.x2=f.x1; f.x1=x; f.y2=f.y1; f.y1=y; return y; };
  class Vocoder {
    constructor(sr, buf, waveform, bands, octave, breath, chord, gate, squeezeChord){
      if(!Number.isInteger(bands)||bands<4||bands>VOC.MAX_BANDS) throw new Error("vocoder bands must be 4 to "+VOC.MAX_BANDS);
      if(!Number.isInteger(octave)||octave<-2||octave>2) throw new Error("vocoder octave must be -2 to 2");
      if(typeof breath!=="number") throw new Error("vocoder breath must be a number");
      if(typeof gate!=="number") throw new Error("vocoder gate must be a number");
      // squeeze_chord: the chord a full squeeze switches to ("none" = one note) (firmware Vocoder.squeeze)
      this.waveform=waveform; this.chord=""; this.keySteps=null; this.squeezed=false;
      const mode=c=> c==null||c==="none" ? null : String(c);
      this.squeezable=squeezeChord!=null; this.modes= this.squeezable ? [mode(chord), mode(squeezeChord)] : [mode(chord)];
      for(const m of this.modes) if(!VOC_KEY_CHORDS[m]) this.wave(m);   // an unknown chord fails here, at load
      this.use(this.modes[0]);
      this.sr=sr; this.oct=Math.pow(2,octave); this.hz=0; this.ph=0; this.breath=clamp(breath,0,1);
      let n=1; while(n*2<=buf) n*=2; this.n=n;
      this.hann=new Float64Array(n); for(let i=0;i<n;i++) this.hann[i]=Math.sin(Math.PI*(i+0.5)/n)**2;
      const psd=2/(n*n*0.375);
      // noise gate (firmware VOCODER_GATE): summed band power, open at the gate, shut GATE_CLOSE dB under it after GATE_HOLD
      this.gate= gate>VOC.GATE_OFF ? 32768*32768*Math.pow(10,gate/10)/psd : null;
      this.gateClose= this.gate===null ? null : this.gate*Math.pow(10,-VOC.GATE_CLOSE/10); this.held=0;
      const high=Math.min(VOC.HIGH,0.4*sr), ratio=Math.pow(high/VOC.LOW,1/(bands-1)), half=Math.sqrt(ratio), Q=1/(half-1/half);
      this.bands=[];
      for(let i=0;i<bands;i++){
        const c=VOC.LOW*Math.pow(ratio,i), lo=c/half, hi=c*half, a=Math.max(1,Math.trunc(lo*n/sr+0.5)), b=Math.max(a+1,Math.trunc(hi*n/sr+0.5));
        this.bands.push({a, b, k:VOC.GAIN*Math.sqrt(psd/(0.5*(1/lo-1/hi))), floor:hi, level:0, f:bandPass(c,Q,sr)});
      }
      this.noise=null;
      if(this.breath>0){
        const cut=Math.min(VOC.BREATH,0.35*sr); let seed=12345; const t=new Float32Array(VOC.NOISE);
        for(let i=0;i<VOC.NOISE;i++){ seed=(Math.imul(seed,1103515245)+12345)&0x7fffffff; t[i]=((seed>>>15)%60001-30000)/32768; }
        this.noise={t, pos:0, a:Math.trunc(cut*n/sr+0.5), b:n/2, k:this.breath*VOC.GAIN*Math.sqrt(psd)/(17320*Math.sqrt(1-cut/(sr/2))), level:0, f:highPass(cut,0.7071,sr)};
      }
    }
    wave(chord){
      if(chord==null){ const {table, density}=carrierWave(this.waveform); return {table, sub:1, density}; }
      const {table, root, density}=chordWave(this.waveform, chord); return {table, sub:1/root, density};
    }
    use(mode){
      this.keySteps=VOC_KEY_CHORDS[mode]||null;
      if(!this.keySteps) this.setChord(mode);
      else if(!Object.entries(VOC_SHAPES).some(([t,nm])=>nm===this.chord&&t.split(",").length===this.keySteps.length)) this.setChord(vocKeyChord(null, this.keySteps));
    }
    squeeze(on){ if(this.squeezable&&on!==this.squeezed){ this.squeezed=on; this.use(this.modes[on?1:0]); } }
    setChord(chord){
      if(chord===this.chord) return;
      const {table, sub, density}=this.wave(chord);
      this.chord=chord; this.table=table; this.sub=sub; this.dk=1/Math.sqrt(density);
    }
    update(win, hz){
      if(hz>0){ hz=clamp(hz*this.oct,30,2000); if(Math.abs(hz-this.hz)>0.002*hz) this.hz=hz; }
      const n=this.n, off=win.length-n, x=new Float64Array(n);
      for(let i=0;i<n;i++) x[i]=win[off+i]*32768*this.hann[i];
      const p=powerSpectrum(x), sum=(a,b)=>{ let t=0; for(let i=a;i<b;i++) t+=p[i]; return t; };
      const f0=this.hz||110; let root=this.dk/Math.sqrt(f0);
      if(this.gate!==null){
        let total=0; for(const bd of this.bands) total+=sum(bd.a,bd.b); if(this.noise) total+=sum(this.noise.a,this.noise.b);
        if(total>=this.gate) this.held=VOC.GATE_HOLD; else if(total<this.gateClose&&this.held) this.held--;
        if(!this.held) root=0;
      }
      for(const bd of this.bands){
        if(bd.floor<f0){ bd.level=0; continue; }
        let lv=Math.min(1,bd.k*Math.sqrt(sum(bd.a,bd.b))*root);
        if(lv<bd.level) lv+=(bd.level-lv)*VOC.RELEASE;
        bd.level=lv;
      }
      if(this.noise) this.noise.level= root ? Math.min(1,this.noise.k*Math.sqrt(sum(this.noise.a,this.noise.b))) : 0;
    }
    tick(){
      const T=this.table, L=T.length, f0=this.hz||110;
      const p=this.ph*L, i=Math.floor(p), fr=p-i, c=T[i%L]+(T[(i+1)%L]-T[i%L])*fr;
      this.ph+=f0*this.sub/this.sr; if(this.ph>=1) this.ph-=1;
      let y=0; for(const bd of this.bands) y+=biq(bd.f, c*bd.level);
      const nz=this.noise; if(nz){ y+=biq(nz.f, nz.t[nz.pos]*nz.level); nz.pos=(nz.pos+1)%nz.t.length; }
      return mixDown(y,MIX_2);
    }
  }

  /* ---------- effects ---------- */
  const PARAMS={
    autotune:["key","scale","speed","humanize","mix","grain_size","density","squeeze_key","squeeze_scale"],
    harmonizer:["chord","inversion","octave","level","double"],
    vocoder:["waveform","bands","octave","breath","mix","chord","gate","squeeze_chord"],
    pitch_shift:["semitones","mix","window","overlap"],
    granular_pitch_shift:["semitones","mix","grain_size","density","spread"],
    distortion:["drive","pre_gain","post_gain","mode","soft_clip","mix"],
    filter:["filter","mix"],
    phaser:["frequency","feedback","mix","stages"],
    chorus:["max_delay_ms","delay_ms","voices","mix"],
    echo:["max_delay_ms","delay_ms","decay","mix","freq_shift"],
    multi_tap_delay:["max_delay_ms","delay_ms","decay","mix","taps"],
    freeverb:["roomsize","damp","mix"],
  };
  const ALIASES={reverb:"freeverb",delay:"echo",multitap_delay:"multi_tap_delay",multitapdelay:"multi_tap_delay",pitchshift:"pitch_shift",granularpitchshift:"granular_pitch_shift",auto_tune:"autotune",harmony:"harmonizer"};
  function effectKey(name){ if(typeof name!=="string") throw new Error("effect name must be a string"); let k=norm(name); k=ALIASES[k]||k; if(!PARAMS[k]) throw new Error("unknown effect: "+name); return k; }
  const dbToGain=d=>Math.pow(10,d/20);
  const MIX_DEFAULT={autotune:1,pitch_shift:1,granular_pitch_shift:1,distortion:1,filter:1,phaser:1,chorus:0.5,echo:0.25,multi_tap_delay:0.25,freeverb:0.5}; // CircuitPython 10.3 defaults

  // harm: the preset's harmonizer or vocoder, which runs inside the autotune stage, {spec, correct, vocoder}
  function makeEffect(spec, ns, host, harm){
    if(!spec||typeof spec!=="object") throw new Error("effect config must be a dict");
    if(!("effect" in spec)) throw new Error('effect config is missing the "effect" key');
    const type=effectKey(spec.effect), sr=host.sr, S=sr/BASE;
    // Like the firmware, leave out parameters it doesn't know instead of failing.
    for(const k in spec) if(k!=="effect"&&k!=="enabled"&&!PARAMS[type].includes(k)) console.warn(type+" has no parameter "+k+"; ignoring it");
    const P=(k,d)=>compile(spec[k]??d, type+" "+k, ns);
    const mixF=P("mix",MIX_DEFAULT[type]);
    const fx={type, latency:0};
    switch(type){
      case "autotune":{
        const key=spec.key==null?"C":String(spec.key).toUpperCase();
        if(!(key in NOTE_IDX)) throw new Error('autotune key must be "C" to "B", e.g. "F#"');
        const scale=spec.scale==null?"chromatic":String(spec.scale).toLowerCase();
        if(!SCALES[scale]) throw new Error("autotune scale must be chromatic, major or minor");
        const speed=P("speed",1), hum=P("humanize",0);
        const grain=num(spec.grain_size,"grain_size",512)*S, density=clamp(Math.round(num(spec.density,"density",2)),1,8);
        // squeeze_key / squeeze_scale: a second key the handle switches to (on at 0.67, back at 0.5, like code.py)
        let alt=null;
        const usesHandle=v=> typeof v==="string" ? v==="$handle" : (v&&typeof v==="object" ? Object.values(v).some(usesHandle) : false);
        if((spec.squeeze_key!=null||spec.squeeze_scale!=null)&&["speed","humanize","mix"].some(k=>usesHandle(spec[k])))
          throw new Error("autotune: the handle can change the key or drive a setting, not both");
        if(spec.squeeze_key!=null||spec.squeeze_scale!=null){
          const ak=spec.squeeze_key==null?key:String(spec.squeeze_key).toUpperCase();
          if(!(ak in NOTE_IDX)) throw new Error('autotune squeeze_key must be "C" to "B", e.g. "F#"');
          const as=spec.squeeze_scale==null?scale:String(spec.squeeze_scale).toLowerCase();
          if(!SCALES[as]) throw new Error("autotune squeeze_scale must be chromatic, major or minor");
          alt=new NoteSnapper(as,NOTE_IDX[ak],host.g);
        }
        const rest=new NoteSnapper(scale,NOTE_IDX[key],host.g);
        const det=new Detector(sr,host.g);
        let snap=rest, squeezed=false;
        const gsh=new CPGranular(grain,density,0), lsh=new LockedShifter(grain);
        let target=0, corr=0, ratio=1;
        fx.latency=grain/2+2;
        // harmonizer: one more grain shifter per harmony voice, summed with the corrected voice
        const correct=!harm||harm.correct;
        let voices=[], ivs=[], keyChord=null, inv=0, oct=0, levelF=null, dbl=0;
        // vocoder: a carrier at the tuned note, with the voice (vocMix 0) or instead of it (1)
        let voc=null, vocMixF=null, vocTurn=0;
        if(harm&&harm.vocoder){
          const vs=harm.spec;
          for(const k in vs) if(k!=="effect"&&k!=="enabled"&&!PARAMS.vocoder.includes(k)) console.warn("vocoder has no parameter "+k+"; ignoring it");
          const usesH=v=> typeof v==="string" ? v==="$handle" : (v&&typeof v==="object" ? Object.values(v).some(usesH) : false);
          if(alt&&usesH(vs.mix)) throw new Error("vocoder: the handle can change the key or drive the mix, not both");
          if(alt&&vs.squeeze_chord!=null) throw new Error("vocoder: the handle can change the key or the chord, not both");
          if(vs.squeeze_chord!=null&&usesH(vs.mix)) throw new Error("vocoder: the handle can change the chord or drive the mix, not both");
          const intOf=(v,d,nm)=>{ v=v==null?d:v; if(typeof v!=="number") throw new Error("vocoder "+nm+" must be a number"); return Math.trunc(v); };
          voc=new Vocoder(sr, det.BUF, vs.waveform==null?"saw":String(vs.waveform), intOf(vs.bands,8,"bands"), intOf(vs.octave,0,"octave"), vs.breath==null?0:vs.breath, vs.chord, vs.gate==null?VOC.GATE:vs.gate, vs.squeeze_chord);
          vocMixF=compile(vs.mix??1,"vocoder mix",ns);
        }
        else if(harm){
          const hs=harm.spec;
          for(const k in hs) if(k!=="effect"&&k!=="enabled"&&!PARAMS.harmonizer.includes(k)) console.warn("harmonizer has no parameter "+k+"; ignoring it");
          const chord=hs.chord==null?"major":String(hs.chord);
          const tones=CHORDS[chord]||KEY_CHORDS[chord];
          if(!tones) throw new Error("unknown harmonizer chord: "+chord);
          inv=hs.inversion==null?0:hs.inversion; oct=hs.octave==null?0:hs.octave;
          if(!Number.isInteger(inv)||inv<0||inv>maxInversion(tones)) throw new Error("harmonizer "+chord+" has no inversion "+hs.inversion);
          if(!Number.isInteger(oct)) throw new Error("harmonizer octave must be a whole number");
          // "double": one more voice an octave above or below the sung note (firmware DOUBLES)
          if(hs.double!=null){
            dbl={above:12,below:-12}[String(hs.double).toLowerCase()]||0;
            if(!dbl) throw new Error("harmonizer double is above or below, not "+hs.double);
            if(tones.length>3) throw new Error("harmonizer double needs a chord of three notes or fewer");
          }
          levelF=compile(hs.level??0.7,"harmonizer level",ns);
          keyChord=KEY_CHORDS[chord]||null;
          ivs=voicing(keyChord ? OFF_KEY_SHAPE.slice(0,tones.length) : tones, inv, oct);
          if(dbl) ivs.push(dbl);
          voices=ivs.map(()=>new CPGranular(grain,density,0));
        }
        let vRatios=ivs.map(iv=>Math.pow(2,iv/12));
        let chordNote=null, chordNext=null, chordCount=0;
        const settleN=Math.max(1, Math.round(0.096*sr/det.BLOCK));
        fx.process=(buf,env)=>{
          // speed s -> per-block rate 0.08 + 0.92 s^2; humanize h -> dead zone of h*30 cents (firmware autotune.py)
          const mix=clamp(mixF(env),0,1), s=clamp(speed(env),0,1), rate=0.08+0.92*s*s, dz=clamp(hum(env),0,1)*0.3;
          const hl=levelF?clamp(levelF(env),0,1):0, hg=1/(1+voices.length*hl);   // keeps the sum about as loud as the voice alone (firmware _set_levels)
          const py=host.g.engine==="python";
          if(alt){
            const h=env.handle||0;
            if(!squeezed&&h>=0.67){ squeezed=true; snap=alt; alt.target=null; }
            else if(squeezed&&h<=0.5){ squeezed=false; snap=rest; rest.target=null; }
          }
          if(voc&&voc.squeezable){
            const h=env.handle||0;
            if(!voc.squeezed&&h>=0.67) voc.squeeze(true); else if(voc.squeezed&&h<=0.5) voc.squeeze(false);
          }
          for(let i=0;i<buf.length;i++){
            const x=buf[i];
            if(det.push(x)){
              const hz=det.detect(), semis= hz>0 ? snap.correction(hz) : null;
              let cents=null;
              if(semis!==null){ cents=-semis*100; target= Math.abs(semis)<=dz ? 0 : semis-Math.sign(semis)*dz; } else target*=0.9;
              if(!correct) target=0;
              corr+=(target-corr)*rate; ratio=Math.pow(2,corr/12);
              if(keyChord&&snap.target!==null){
                // the chord moves to a new note only once it has held ~100 ms (firmware HARMONY_SETTLE)
                const note=snap.target;
                // by pitch class, so a note read an octave out is the same chord; an offset of an octave or
                // more can only be such a misreading and is folded back (firmware Autotune._chord_intervals)
                const pc=((note%12)+12)%12;
                if(chordNote===null||pc===((chordNote%12)+12)%12){ if(chordNote===null) chordNote=note; chordNext=null; }
                else if(pc===chordNext){ if(++chordCount>=settleN){ chordNote=note; chordNext=null; } }
                else { chordNext=pc; chordCount=1; }
                let off=chordNote-note; while(off>11) off-=12; while(off<-11) off+=12;
                ivs=keyIntervals(note+off, snap, keyChord, inv, oct).map(iv=>off+iv);
                if(dbl) ivs.push(dbl);
              }
              vRatios=ivs.map(iv=>Math.pow(2,(corr+iv)/12));
              if(py&&hz>0) lsh.lock(sr/hz); lsh.begin(det.BLOCK);
              // the mic measures the voice on the blocks between its pitch searches, every other block
              // a key chord follows the snapped note (firmware: on pitch searches, no settling)
              if(voc&&voc.keySteps&&snap.target!==null) voc.setChord(vocKeyChord(keyTones(snap.target, snap, voc.keySteps), voc.keySteps));
              if(voc&&(vocTurn^=1)===0) voc.update(det.win, hz>0 ? hz*ratio : 0);
              host.report({hz, cents: correct?cents:null, correction:corr, target: correct?snap.target:null, midi: hz>0?12*log2(hz/440)+69:null, autotune:correct, squeezed, harmony: voices.length?ivs.slice():null});
            }
            let out= py ? x*(1-mix)+lsh.tick(x,ratio)*mix : shiftMix(x,gsh.tick(x,ratio),mix);
            if(voices.length){
              let hsum=0; for(let k=0;k<voices.length;k++) hsum+=shiftMix(x,voices[k].tick(x,vRatios[k]),1);
              out=(out+hsum*hl)*hg;
            }
            if(voc){ const vm=clamp(vocMixF(env),0,1); out=vm>=1 ? voc.tick() : mixDown(out*(1-vm)+voc.tick()*vm,MIX_2); }
            buf[i]=out;
          }
        };
        break;
      }
      case "pitch_shift": case "granular_pitch_shift":{
        const semi=P("semitones",0);
        // window / overlap (bytes) and grain_size (samples) scale with the rate, as firmware at_rate() does
        const sh= type==="pitch_shift"
          ? new CPPitch(num(spec.window,"window",1024)*S, num(spec.overlap,"overlap",128)*S)
          : new CPGranular(num(spec.grain_size,"grain_size",1024)*S, num(spec.density,"density",2), num(spec.spread,"spread",0));
        fx.latency= type==="pitch_shift" ? sh.ws/2 : sh.g;
        fx.process=(buf,env)=>{ const r=Math.pow(2,semi(env)/12), mix=clamp(mixF(env),0,1);
          for(let i=0;i<buf.length;i++){ const x=buf[i]; buf[i]=shiftMix(x,sh.tick(x,r),mix); } };
        break;
      }
      case "distortion":{
        // Step for step audiofilters.Distortion (CircuitPython 10, shared-module/audiofilters/Distortion.c),
        // on int16-scaled words. Overdrive ignores drive there: pre_gain is what pushes it.
        const drive=P("drive",0), pre=P("pre_gain",0), post=P("post_gain",0);
        let mode=norm(spec.mode==null?"clip":spec.mode); if(mode==="wavefold") mode="waveshape";
        if(!["clip","lofi","overdrive","waveshape"].includes(mode)) throw new Error("unknown distortion mode: "+spec.mode);
        if(spec.soft_clip!=null&&typeof spec.soft_clip!=="boolean") throw new Error("soft_clip must be true or false");
        const soft=!!spec.soft_clip;
        fx.process=(buf,env)=>{
          let d=clamp(drive(env),0,1); const g1=dbToGain(clamp(pre(env),-60,60)), g2=dbToGain(clamp(post(env),-80,24)), mix=clamp(mixF(env),0,1);
          if(mix<=0.01) return;
          let mask=0;
          if(mode==="clip") d=1.0001-d;
          else if(mode==="waveshape") d=2*d/(1.0001-d);
          else if(mode==="lofi") mask=~((1<<Math.round(d*14))-1);
          for(let i=0;i<buf.length;i++){
            const sw=Math.max(-32768,Math.min(32767,Math.round(buf[i]*32768)));
            let w=Math.trunc(sw*g1);
            if(mode==="lofi") w&=mask;
            if(mode!=="lofi"||soft){
              let f=w/32768;
              if(mode==="clip"){ f=Math.pow(Math.abs(f),d); if(w<0) f=-f; }
              else if(mode==="overdrive"){ f*=0.686306; const z=1+Math.exp(Math.sqrt(Math.abs(f))*-0.75), e=Math.exp(f); f=(e-Math.exp(-f*z))/(e+Math.exp(-f)); }
              else if(mode==="waveshape") f=(1+d)*f/(1+d*Math.abs(f));
              f*=g2;
              if(soft) f=f>0 ? 1-Math.exp(-f) : -1+Math.exp(f);
              w=Math.trunc(f*32767);
            } else w=Math.trunc(w*g2);
            if(!soft) w=Math.min(Math.max(w,-32767),32768);
            buf[i]=Math.max(-32768,Math.min(32767,Math.trunc(sw*(1-mix)+w*mix)))/32768;
          }
        };
        break;
      }
      case "filter":{
        let list=spec.filter; if(list==null) throw new Error("filter requires a filter");
        if(!Array.isArray(list)) list=[list];
        const MODES=["low_pass","high_pass","band_pass","notch","low_shelf","high_shelf","peaking_eq"];
        const bq=list.map(f=>{
          if(!f||typeof f!=="object") throw new Error("filter must be a dict or list of dicts");
          const m=norm(f.mode==null?"low_pass":f.mode); if(!MODES.includes(m)) throw new Error("unknown filter mode: "+f.mode);
          if(!("frequency" in f)) throw new Error("filter requires a frequency");
          return {m, fr:compile(f.frequency,"frequency",ns), Q:compile(f.Q??0.7071,"Q",ns), A:compile(f.A??1,"A",ns), x1:0,x2:0,y1:0,y2:0};
        });
        const coef=(b,env)=>{
          const f=clamp(b.fr(env),10,sr*0.49), Q=Math.max(0.05,b.Q(env)), A=Math.max(0.01,b.A(env));
          const w=2*Math.PI*f/sr, cs=Math.cos(w), sn=Math.sin(w), al=sn/(2*Q); let b0,b1,b2,a0,a1,a2;
          switch(b.m){
            case "low_pass": b0=(1-cs)/2; b1=1-cs; b2=b0; a0=1+al; a1=-2*cs; a2=1-al; break;
            case "high_pass": b0=(1+cs)/2; b1=-(1+cs); b2=b0; a0=1+al; a1=-2*cs; a2=1-al; break;
            case "band_pass": b0=al; b1=0; b2=-al; a0=1+al; a1=-2*cs; a2=1-al; break;
            case "notch": b0=1; b1=-2*cs; b2=1; a0=1+al; a1=-2*cs; a2=1-al; break;
            case "peaking_eq": b0=1+al*A; b1=-2*cs; b2=1-al*A; a0=1+al/A; a1=-2*cs; a2=1-al/A; break;
            default:{ const sq=2*Math.sqrt(A)*al;
              if(b.m==="low_shelf"){ b0=A*((A+1)-(A-1)*cs+sq); b1=2*A*((A-1)-(A+1)*cs); b2=A*((A+1)-(A-1)*cs-sq); a0=(A+1)+(A-1)*cs+sq; a1=-2*((A-1)+(A+1)*cs); a2=(A+1)+(A-1)*cs-sq; }
              else { b0=A*((A+1)+(A-1)*cs+sq); b1=-2*A*((A-1)+(A+1)*cs); b2=A*((A+1)+(A-1)*cs-sq); a0=(A+1)-(A-1)*cs+sq; a1=2*((A-1)-(A+1)*cs); a2=(A+1)-(A-1)*cs-sq; } }
          }
          b.b0=b0/a0; b.b1=b1/a0; b.b2=b2/a0; b.a1=a1/a0; b.a2=a2/a0;
        };
        fx.process=(buf,env)=>{
          const mix=clamp(mixF(env),0,1); bq.forEach(b=>coef(b,env));
          for(let i=0;i<buf.length;i++){ const x=buf[i]; let y=x;
            for(const b of bq){ const o=b.b0*y+b.b1*b.x1+b.b2*b.x2-b.a1*b.y1-b.a2*b.y2; b.x2=b.x1; b.x1=y; b.y2=b.y1; b.y1=o; y=o; }
            buf[i]=mix<=0.01 ? x : mixDown(x*(1-mix)+y*mix,MIX_2); }
        };
        break;
      }
      case "phaser":{
        // audiofilters.Phaser, step for step (Q15 saturation as float clamps): feedback is held to 0.1..0.9,
        // the all-pass coefficient is (1-f)/(1+f) with f the frequency over Nyquist, out = dry + wet*mix, near-hard limited.
        const freq=P("frequency",1000), fbk=P("feedback",0.7), stages=Math.max(1,Math.round(num(spec.stages,"stages",6)));
        const ap=new Float32Array(stages); let last=0;
        fx.process=(buf,env)=>{
          const ny=sr/2, f=clamp(freq(env),0,ny)/ny, c=Math.trunc((1-f)/(1+f)*32767)/32768, fb=Math.trunc(clamp(fbk(env),0.1,0.9)*32767)/32768, mix=Math.trunc(clamp(mixF(env),0,1)*32767)/32768;
          if(mix<=328/32768) return;
          for(let i=0;i<buf.length;i++){ const x=buf[i]; let w=sat(x+sat(last*fb));
            for(let j=0;j<stages;j++){ const a=sat(sat(w*-c)+ap[j]); ap[j]=sat(sat(a*c)+w); w=a; }
            last=w; buf[i]=mixDown(x+sat(w*mix),MIX_HARD); }
        };
        break;
      }
      case "chorus": case "echo": case "multi_tap_delay":{
        const maxMs=num(spec.max_delay_ms,"max_delay_ms", type==="chorus"?50:500);   // ASSUMED defaults
        const N=Math.max(4,Math.ceil(maxMs/1000*sr)+4), line=new Float32Array(N); let w=0;
        const delay=P("delay_ms", type==="chorus"?50:(type==="echo"?250:500)), decay=type==="chorus"?null:P("decay",0.7), voices=type==="chorus"?P("voices",1):null;
        if(type==="echo"&&spec.freq_shift!=null&&typeof spec.freq_shift!=="boolean") throw new Error("freq_shift must be true or false");
        let taps=[[1,1]];
        if(type==="multi_tap_delay"&&spec.taps!=null){
          if(!Array.isArray(spec.taps)) throw new Error("taps must be a list");
          taps=spec.taps.map(t=>{ if(Array.isArray(t)){ if(t.length!==2) throw new Error("taps pairs must be [position, level]"); return [+t[0],+t[1]]; } return [+t,1]; });
        }
        const read=d=>{ let r=w-d; while(r<0) r+=N; const i=Math.floor(r), f=r-i; return line[i%N]+(line[(i+1)%N]-line[i%N])*f; };
        fx.process=(buf,env)=>{
          const d=clamp(delay(env)/1000*sr,1,N-3), mix=clamp(mixF(env),0,1);
          if(type==="chorus"){
            // CircuitPython's Chorus: voices sit at fixed, evenly spaced delays from 0 to delay_ms (the first
            // is the dry sample itself), are summed rather than averaged, and the sum is added to the dry
            // signal at full level: out = dry + voices*mix
            const v=Math.max(1,Math.round(voices(env))), len=Math.max(1,Math.floor(d)), step=v>1?Math.floor(len/(v-1))-1:0, sc=mixScale(v);
            for(let i=0;i<buf.length;i++){ const x=buf[i]; line[w]=x; let wet=0;
              if(v===1) wet=x; else { for(let k=0;k<v;k++){ let r=w-k*step; while(r<0) r+=N; wet+=line[r]; } wet=mixDown(wet,sc); }
              w=(w+1)%N; buf[i]=mixDown(x+wet*mix,MIX_HARD); }
          }else if(mix<=0.01){
            // CircuitPython's Echo passes the dry sample straight through and leaves its buffer alone
          }else{
            // CircuitPython's Echo and MultiTapDelay: the doubled mix law (dry at full until mix 0.5, the
            // echo at full from 0.5), with the soft limiter on the buffer and on the output
            const dc=clamp(decay(env),0,0.98), dry=Math.min(2-2*mix,1), eff=Math.min(2*mix,1), sc=mixScale(taps.length);
            for(let i=0;i<buf.length;i++){ const x=buf[i]; let wet=0;
              for(const [pos,lev] of taps) wet+=read(clamp(pos*d,1,N-3))*lev;
              if(taps.length>1) wet=mixDown(wet,sc);
              line[w]=mixDown(x+read(d)*dc,MIX_2); w=(w+1)%N; buf[i]=mixDown(x*dry+wet*eff,MIX_2); }
          }
        };
        break;
      }
      case "freeverb":{
        const room=P("roomsize",0.5), damp=P("damp",0.5);   // ASSUMED defaults
        // CircuitPython's Freeverb, step for step. Its comb and all-pass lengths are fixed sample counts,
        // played at the mic's own rate (host.g.rate, 24 kHz by default) rather than 44.1 kHz
        const k=sr/(host.g.rate||24000), combs=[1116,1188,1277,1356,1422,1491,1557,1617].map(n=>({b:new Float32Array(Math.max(8,Math.round(n*k))),i:0,f:0})),
          aps=[556,441,341,225].map(n=>({b:new Float32Array(Math.max(8,Math.round(n*k))),i:0}));
        fx.process=(buf,env)=>{
          const fb=clamp(room(env),0,1)*0.28+0.7, d1=clamp(damp(env),0,1)*0.4, d2=1-d1, mix=clamp(mixF(env),0,1),
            dry=Math.min(2-2*mix,1), eff=Math.min(2*mix,1);
          for(let i=0;i<buf.length;i++){ const x=buf[i], inp=sat(x*8738/131072); let sum=0;
            for(const c of combs){ const o=c.b[c.i]; sum+=o; c.f=sat(o*d2+c.f*d1); c.b[c.i]=sat(inp+sat(c.f*fb)); c.i=(c.i+1)%c.b.length; }
            let out=sat(sum*0.24);
            for(const a of aps){ const bo=a.b[a.i]; a.b[a.i]=out+bo*0.5; out=sat((bo-out)/2); a.i=(a.i+1)%a.b.length; }
            buf[i]=mixDown(sat(x*dry)+sat(out*30*eff),MIX_2); }
        };
        break;
      }
    }
    return fx;
  }

  // create_effects(): skips "enabled": false; autotune is moved to the front, as the firmware does
  function buildChain(preset, host, isSample){
    if(!preset) return {fx:[], notes:[]};
    let specs, defs;
    if(Array.isArray(preset)){ specs=preset; defs=null; }
    else if(typeof preset==="object"){ specs=preset.list||[]; defs=preset.blocks; }
    else throw new Error("preset must be an object");
    if(!Array.isArray(specs)) throw new Error('"list" must be a list of effects');
    const notes=[];
    let enabled=specs.filter((s,i)=>{ if(s&&"enabled" in s&&typeof s.enabled!=="boolean") throw new Error("specs["+i+"] enabled must be true or false"); return !(s&&s.enabled===false); });
    const isType=t=>s=>s&&typeof s.effect==="string"&&(()=>{ try{ return effectKey(s.effect)===t; }catch(e){ return false; } })();
    const at=enabled.filter(isType("autotune")), hz=enabled.filter(isType("harmonizer")), vo=enabled.filter(isType("vocoder"));
    if(at.length){
      if(isSample) throw new Error("autotune is not allowed in sample effects");
      if(at.length>1) throw new Error("only one autotune per preset");
      if(enabled[0]!==at[0]){ enabled=[at[0]].concat(enabled.filter(s=>s!==at[0])); notes.push("autotune moved to the front of the chain, as the firmware does"); }
    }
    let harm=null;
    if(hz.length){
      if(isSample) throw new Error("the harmonizer is not allowed in sample effects");
      if(hz.length>1) throw new Error("only one harmonizer per preset");
      // the harmonizer runs inside the autotune stage; without autotune the voice stays untuned
      harm={spec:hz[0], correct:at.length>0};
      enabled=enabled.filter(s=>s!==hz[0]);
      if(!at.length) enabled=[{effect:"autotune"}].concat(enabled);
    }
    if(vo.length){
      if(isSample) throw new Error("the vocoder is not allowed in sample effects");
      if(vo.length>1) throw new Error("only one vocoder per preset");
      if(hz.length) throw new Error("a preset can have a harmonizer or a vocoder, not both");
      // the vocoder runs inside the autotune stage too; without autotune the carrier follows the voice as sung
      harm={spec:vo[0], correct:at.length>0, vocoder:true};
      enabled=enabled.filter(s=>s!==vo[0]);
      if(!at.length) enabled=[{effect:"autotune"}].concat(enabled);
    }
    const ns=new NS(defs, host.blocks);
    return {fx:enabled.map((s,i)=>makeEffect(s,ns,host,i===0?harm:null)), notes};
  }
  function validateConfig(cfg){
    const issues=[];
    if(!cfg||typeof cfg!=="object"||Array.isArray(cfg)) return [{where:"config", msg:"config.json must be an object"}];
    const presets=cfg.presets==null?[]:cfg.presets, samples=cfg.samples==null?[]:cfg.samples;
    if(!Array.isArray(presets)) issues.push({where:"presets",msg:"presets must be a list"});
    if(!Array.isArray(samples)) issues.push({where:"samples",msg:"samples must be a list"});
    const host={sr:BASE, g:{detector:"yin",minRms:250,minHz:80,maxHz:500,vt:0.15,hyst:0.3,engine:"native"}, blocks:{handle:()=>0}, report:()=>{}};
    (Array.isArray(presets)?presets:[]).slice(0,4).forEach((p,i)=>{ try{ const c=buildChain(p,host,false); c.notes.forEach(n=>issues.push({where:"preset "+(i+1),msg:n,soft:true})); }catch(e){ issues.push({where:"preset "+(i+1), msg:e.message}); } });
    (Array.isArray(samples)?samples:[]).slice(0,4).forEach((s,i)=>{
      try{
        if(!s||typeof s!=="object") throw new Error("sample must be an object");
        if(typeof s.file!=="string") throw new Error('sample needs a "file" name');
        const pm=s.playmode==null?"oneshot":String(s.playmode).toLowerCase(); if(!["oneshot","hold","startstop"].includes(pm)) throw new Error("unknown playmode: "+s.playmode);
        buildChain({list:s.effects||[], blocks:s.blocks}, host, true);
      }catch(e){ issues.push({where:"sample "+(i+1), msg:e.message}); }
    });
    if(Array.isArray(presets)&&presets.length>4) issues.push({where:"presets",msg:"only the first 4 presets are used",soft:true});
    if(Array.isArray(samples)&&samples.length>4) issues.push({where:"samples",msg:"only the first 4 samples are used",soft:true});
    return issues;
  }

  /* ---------- the device: mic chain (gated by the handle) + sample voice ---------- */
  class TingDSP {
    constructor(sr){
      this.sr=sr; this.S=sr/BASE;
      this.g={rate:24000, sampleLevel:1, power:true, gate:false, handle:0, level:0.4, engine:"native", detector:"yin", hyst:0.3, vt:0.15, minRms:250, minHz:80, maxHz:500};
      this.host={sr, g:this.g, blocks:{handle:()=>this.g.handle}, report:i=>{ this.atInfo=i; }};
      this.chain=[]; this.samples=[]; this.voice=null;
      this.tuner=new Detector(sr,this.g); this.tunerInfo=null; this.atInfo=null;
      this.env={frame:0, dt:0, handle:0};
      this.gateG=0; this.lvl=0; this.mic=new Float32Array(128); this.smp=new Float32Array(128);
      this.n=0; this.ping=null; this.pingResult=null; this.error=null; this.latency=0; this.info=null;
      this.rec=null; this.recDone=null; this.through=false; this.plev=1;     // on-device recorder: raw mic, up to maxSec
    }
    set(q){
      if(q.g) Object.assign(this.g,q.g);
      if("preset" in q){
        try{ const c=buildChain(q.preset,this.host,false); this.chain=c.fx; this.error=null; }
        catch(e){ this.chain=[]; this.error=e.message; }           // the firmware falls back to clean
        // "level": the preset's output level, 0..1 (the firmware sets the output mixer voice to it)
        const lv=q.preset&&q.preset.level; this.plev=typeof lv==="number"&&lv>=0&&lv<=1&&!this.error ? lv : 1;
        this.atInfo=null; this.latency=this.chain.reduce((s,f)=>s+f.latency,0);
      }
      if("sampleFx" in q) this.through=String(q.sampleFx||"own").toLowerCase()==="preset";
      if(q.samples){
        this.samples=q.samples.map(s=>{ if(!s) return null; let fx=[]; try{ fx=buildChain({list:s.effects||[],blocks:s.blocks},this.host,true).fx; }catch(e){}
          return {data:s.data, fx}; });
        this.voice=null;
      }
      if(q.play!=null){ const s=this.samples[q.play.idx]; if(s&&s.data) this.voice={s, pos:0, loop:q.play.loop}; }
      if(q.stopSample) this.voice=null;
      if(q._ping) this.ping={id:q._ping, phase:"quiet", t0:this.n, peak:0, at:0};
      if(q.recStart) this.rec={slot:q.recStart.slot, buf:new Float32Array(Math.round(q.recStart.maxSec*this.sr)), len:0};
      if(q.recStop&&this.rec) this.finishRec();
    }
    finishRec(){ const r=this.rec; this.rec=null; this.recDone={slot:r.slot, data:r.buf.slice(0,r.len), sr:this.sr}; }
    // Loopback latency test: listen to the room, play one click, count samples until the mic hears it.
    pingSample(x){
      const g=this.ping, S=this.S, quiet=Math.round(1600*S), clickLen=Math.round(12*S);
      if(g.phase==="quiet"){ g.peak=Math.max(g.peak,Math.abs(x)); if(this.n-g.t0>=quiet){ g.phase="wait"; g.at=this.n; g.thr=Math.max(0.03,g.peak*4); } return 0; }
      const k=this.n-g.at;
      if(k>Math.round(4*S)&&Math.abs(x)>g.thr){ this.pingResult={id:g.id, samples:k, sr:this.sr}; this.ping=null; return 0; }
      if(k>this.sr){ this.pingResult={id:g.id, samples:-1, sr:this.sr}; this.ping=null; return 0; }
      return k<clickLen ? (k%4<2?0.9:-0.9) : 0;
    }
    process(inp,out){
      const n=out.length, g=this.g;
      if(this.mic.length!==n){ this.mic=new Float32Array(n); this.smp=new Float32Array(n); }
      const env=this.env; env.frame++; env.dt=n/this.sr; env.handle=g.handle;
      const mic=this.mic; if(inp.length===n) mic.set(inp); else mic.fill(0);
      if(this.rec){ const r=this.rec, k=Math.min(n,r.buf.length-r.len); r.buf.set(mic.subarray(0,k),r.len); r.len+=k; if(r.len>=r.buf.length) this.finishRec(); }
      // tuner display when the preset has no autotune of its own
      if(!this.chain.some(f=>f.type==="autotune")){
        for(let i=0;i<n;i++) if(this.tuner.push(mic[i])){ const hz=this.tuner.detect(); this.tunerInfo={hz, cents:null, correction:0, target:null, midi:hz>0?12*log2(hz/440)+69:null, autotune:false}; }
      }
      const smp=this.smp; smp.fill(0); let playing=-1;
      if(this.voice){
        const v=this.voice, d=v.s.data; playing=this.samples.indexOf(v.s);
        for(let i=0;i<n;i++){ if(v.pos>=d.length){ if(v.loop) v.pos=0; else { this.voice=null; break; } } smp[i]=d[v.pos++]*g.sampleLevel; }
      }
      const gGoal=g.gate?1:0, lGoal=g.power?g.level:0, through=this.through&&!this.rec;
      // sample_fx "preset": the sample is mixed in ahead of the preset (autotune included), and the handle gates only the mic
      if(through) for(let i=0;i<n;i++){ this.gateG+=(gGoal-this.gateG)*0.004; mic[i]=mic[i]*this.gateG+smp[i]; }
      for(const f of this.chain) f.process(mic,env);
      // every sample chain keeps running so echo and reverb tails ring out after the sample stops
      if(!through) for(const s of this.samples) if(s) for(const f of s.fx){ if(this.voice&&this.voice.s===s) f.process(smp,env); }
      for(let i=0;i<n;i++){
        if(!through) this.gateG+=(gGoal-this.gateG)*0.004;
        this.lvl+=(lGoal-this.lvl)*0.002;
        const y=(through ? mic[i]*this.plev : mic[i]*this.gateG*this.plev+smp[i])*this.lvl;
        out[i]= this.ping ? this.pingSample(inp[i]||0) : y;
        this.n++;
      }
      const ti=this.chain.some(f=>f.type==="autotune") ? this.atInfo : this.tunerInfo;
      this.info=Object.assign({}, ti||{hz:0,midi:null,correction:0,cents:null,target:null,autotune:false},
        {ping:this.pingResult, latency:this.latency, sr:this.sr, playing, error:this.error,
         recLeft:this.rec?1-this.rec.len/this.rec.buf.length:null, rec:this.recDone});
    }
  }
  return {TingDSP, validateConfig, effectKey, CHORDS, KEY_CHORDS, voicing, maxInversion};
}

/* Build the audio engine: an AudioWorklet running TingDSP, or a ScriptProcessor
 * fallback where worklets can't load. Returns {node, kind, set(msg), onInfo}. */
async function makeTingEngine(context){
  const src=`const LIB=(${dspFactory.toString()})(); const TingDSP=LIB.TingDSP;
  class P extends AudioWorkletProcessor{
    constructor(){super(); this.d=new TingDSP(sampleRate); this.n=0; this.port.onmessage=e=>this.d.set(e.data);}
    process(ins,outs){const i=ins[0]&&ins[0][0], o=outs[0][0]; if(!o) return true;
      this.d.process(i||new Float32Array(o.length), o);
      for(let c=1;c<outs[0].length;c++) outs[0][c].set(o);
      if(++this.n%4===0 && this.d.info) this.port.postMessage(this.d.info);
      return true;}
  }
  registerProcessor('ting-dsp',P);`;
  const urls=[()=>URL.createObjectURL(new Blob([src],{type:"application/javascript"})),
    ()=>"data:application/javascript;charset=utf-8,"+encodeURIComponent(src)];
  let loaded=false;
  for(const u of urls){ try{ await context.audioWorklet.addModule(u()); loaded=true; break; }catch(e){} }
  try{
    if(!loaded) throw new Error("no worklet");
    const node=new AudioWorkletNode(context,'ting-dsp',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
    const eng={node, kind:"AudioWorklet", set:p=>node.port.postMessage(p), onInfo:null};
    node.port.onmessage=e=>eng.onInfo&&eng.onInfo(e.data);
    return eng;
  }catch(err){
    const d=new (dspFactory().TingDSP)(context.sampleRate);
    const node=context.createScriptProcessor(512,1,1);
    const eng={node, kind:"ScriptProcessor", set:p=>d.set(p), onInfo:null};
    node.onaudioprocess=e=>{ d.process(e.inputBuffer.getChannelData(0), e.outputBuffer.getChannelData(0)); if(d.info&&eng.onInfo) eng.onInfo(d.info); };
    return eng;
  }
}

/* A synthetic "ah" singer, a little off pitch with scoops and vibrato, for trying presets without a mic. */
function makeSampleSinger(rate){
  // Synthetic "ah" singer: harmonics through vowel formants, off pitch with scoops and vibrato.
  const notes=[[57,-30],[60,24],[62,-34],[64,20],[62,-26],[60,32],[55,-22],[57,28]];
  const dur=0.75, gap=0.12, len=Math.ceil(rate*notes.length*(dur+gap));
  const b=new AudioBuffer({length:len, sampleRate:rate, numberOfChannels:1}), x=b.getChannelData(0);
  const F=[[730,90,1],[1090,110,0.5],[2440,170,0.25]];
  const amp=f=>F.reduce((s,[fc,bw,g])=>s+g/(1+Math.pow((f-fc)/bw,2)),0)+0.02;
  const ph=new Float64Array(41); let t0=0;
  const mhz=m=>440*Math.pow(2,(m-69)/12);
  for(const [m,c] of notes){
    const n0=Math.round(t0*rate), n1=n0+Math.round(dur*rate), base=mhz(m+c/100);
    for(let n=n0;n<n1;n++){
      const t=(n-n0)/rate, scoop=Math.max(0,1-t/0.09)*-0.6, vib=0.15*Math.sin(2*Math.PI*5.2*t)*Math.min(1,t/0.4);
      const f0=base*Math.pow(2,(scoop+vib)/12), env=Math.min(1,t/0.03)*Math.min(1,(dur-t)/0.06);
      let s=0;
      for(let h=1;h<=40;h++){ const fh=f0*h; if(fh>rate*0.45) break; ph[h]+=2*Math.PI*fh/rate; s+=amp(fh)*Math.sin(ph[h])/Math.sqrt(h); }
      x[n]=s*env*0.22;
    }
    t0+=dur+gap;
  }
  return b;
}
