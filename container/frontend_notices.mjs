import { readFileSync, writeFileSync } from 'node:fs';

const packages = ['react', 'react-dom', 'scheduler'];
const notices = packages.map(name => {
  const manifest = JSON.parse(readFileSync(`node_modules/${name}/package.json`, 'utf8'));
  return `${name} ${manifest.version}\n${readFileSync(`node_modules/${name}/LICENSE`, 'utf8')}`;
});
writeFileSync('dist/assets/third-party-notices.txt', notices.join('\n\n'));
