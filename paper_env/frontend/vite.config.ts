import { defineConfig } from 'vite';

// Built files are served by the FastAPI app under /ui/ (see paper_env/api/main.py).
// @jupyter-widgets/controls reads its version with a CommonJS require('../package.json'), which a browser bundle lacks.
const fixControlsRequire = {
  name: 'fix-controls-require',
  transform(code: string, id: string) {
    if (id.includes('@jupyter-widgets/controls/lib/index.js')) return code.replace("require('../package.json').version", "'5.0.0'");
  },
};

export default defineConfig({
  plugins: [fixControlsRequire],
  base: './',  // relative, so it works at /ui/ and when mounted at /paper-env/ui/
  build: { outDir: '../paper_env/api/static/app', emptyOutDir: true, chunkSizeWarningLimit: 4000 },
  server: { port: 5173, proxy: { '/paper-envs': 'http://127.0.0.1:8000', '/runs': 'http://127.0.0.1:8000', '/cells': 'http://127.0.0.1:8000',
    '/cell-versions': 'http://127.0.0.1:8000', '/datasets': 'http://127.0.0.1:8000', '/checkpoints': 'http://127.0.0.1:8000',
    '/blocked': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000', '/events': { target: 'ws://127.0.0.1:8000', ws: true } } },
});
