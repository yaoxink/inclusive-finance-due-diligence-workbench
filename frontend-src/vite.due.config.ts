import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import {fileURLToPath} from 'node:url';
import {resolve, relative, isAbsolute} from 'node:path';

const frontendRoot=fileURLToPath(new URL('.',import.meta.url));
const workspaceRoot=resolve(frontendRoot,'../..');
const outDir=resolve(frontendRoot,'../../due-diligence-app/frontend-dist');
const expectedOutDir=resolve(workspaceRoot,'due-diligence-app','frontend-dist');
const relativeOutput=relative(workspaceRoot,outDir);
if(outDir!==expectedOutDir||relativeOutput.startsWith('..')||isAbsolute(relativeOutput)) {
  throw new Error('Dedicated due-diligence output must stay in due-diligence-app/frontend-dist.');
}

export default defineConfig({
  root:frontendRoot,
  base:'/',
  plugins:[react()],
  build:{
    outDir,
    // This target is separate from the existing meeting/benchmark dist.
    // Never let this build recursively clear an existing directory.
    emptyOutDir:false,
    rollupOptions:{input:resolve(frontendRoot,'due-diligence.html')},
  },
  server:{host:'127.0.0.1',proxy:{'/api/due-diligence':'http://127.0.0.1:8769'}},
});
