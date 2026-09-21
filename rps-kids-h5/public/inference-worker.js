/* Classic worker: MediaPipe's WASM loader uses importScripts. No frames leave this worker. */
importScripts('./vendor/vision_bundle.js');
let recognizer;

function verifyWebGl() {
  const canvas=new OffscreenCanvas(1,1);
  const gl=canvas.getContext('webgl2') || canvas.getContext('webgl');
  if(!gl || typeof gl.activeTexture!=='function') {
    throw Error('WebGL 不可用：MediaPipe 需要可用的 OffscreenCanvas WebGL 上下文');
  }
  const debug=gl.getExtension('WEBGL_debug_renderer_info');
  return {
    version:gl.getParameter(gl.VERSION),
    renderer:debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
  };
}

self.onmessage = async ({data}) => {
  try {
    if(data.type==='init') {
      recognizer?.close();
      const graphics=verifyWebGl();
      const files=await vision.FilesetResolver.forVisionTasks(new URL('./vendor/wasm', self.location.href).href);
      recognizer=await vision.GestureRecognizer.createFromOptions(files, {
        baseOptions:{modelAssetPath:new URL('./models/gesture_recognizer.task',self.location.href).href,delegate:'CPU'},
        runningMode:'VIDEO',numHands:2,
        cannedGesturesClassifierOptions:{maxResults:-1,scoreThreshold:0},
        minHandDetectionConfidence:0.5,minHandPresenceConfidence:0.5,minTrackingConfidence:0.5,
      });
      self.postMessage({type:'ready',graphics});
    } else if(data.type==='frame') {
      try {
        const begin=performance.now();
        const result=recognizer.recognizeForVideo(data.bitmap,data.timestamp);
        self.postMessage({type:'result',result,ms:performance.now()-begin,timestamp:data.timestamp});
      } finally { data.bitmap.close(); }
    }
  } catch(error) { self.postMessage({type:'error',message:error.message || String(error)}); }
};
