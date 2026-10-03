import { cp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';

// ponytail: copy only Cesium's required runtime assets, never upstream scene data.
await mkdir('dist/cesium', { recursive: true });
for (const name of ['Workers', 'ThirdParty', 'Assets', 'Widgets']) {
  await cp(`node_modules/cesium/Build/Cesium/${name}`, `dist/cesium/${name}`, { recursive: true });
}
await cp('vendor/gods-eye-view/LICENSE', 'dist/GODSEYE-LICENSE.txt');
await cp('vendor/gods-eye-view/PROVENANCE.json', 'dist/GODSEYE-PROVENANCE.json');
await cp('node_modules/cesium/LICENSE.md', 'dist/CESIUM-LICENSE.md');
await cp('node_modules/cesium/ThirdParty.json', 'dist/CESIUM-THIRD-PARTY.json');
const lock = JSON.parse(await readFile('package-lock.json', 'utf8'));
const notices = ['God’s Eye View\n' + await readFile('vendor/gods-eye-view/LICENSE', 'utf8'),
  'Simple Icons @ d9ea58066506bc80da65d5516813636b22b58a06\n' + await readFile('public/brand-icons/LICENSE.md', 'utf8') + '\n' + await readFile('public/brand-icons/DISCLAIMER.md', 'utf8')];
for (const [path, metadata] of Object.entries(lock.packages)) {
  if (!path || metadata.dev || metadata.optional) continue;
  const licenses = (await readdir(path)).filter((name) => /^(license|licence|copying|notice)(\.|$)/i.test(name));
  notices.push(`${path} @ ${metadata.version}\nLicense: ${metadata.license}\n` + (await Promise.all(licenses.map((name) => readFile(`${path}/${name}`, 'utf8')))).join('\n'));
}
await writeFile('dist/THIRD-PARTY-NOTICES.txt', notices.join('\n\n' + '='.repeat(72) + '\n\n'));
