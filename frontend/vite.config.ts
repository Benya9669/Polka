import {createHash} from 'node:crypto';
import {readFile, readdir, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {defineConfig} from 'vite';

export default defineConfig({plugins:[{
  name:'offline-shell',
  async writeBundle({dir = 'dist'}) {
    const assets = (await readdir(join(dir, 'assets'))).sort().map(name => '/assets/' + name);
    const source = await readFile(new URL('./offline-worker.js', import.meta.url), 'utf8');
    const index = await readFile(join(dir, 'index.html'));
    const release = createHash('sha256').update(source).update(index).update(assets.join('\n')).digest('hex').slice(0, 16);
    await writeFile(join(dir, 'offline-worker.js'),
      `const SHELL_CACHE = 'polka-shell-${release}';\nconst ASSETS = ${JSON.stringify(assets)};\n${source}`);
  },
}]});
