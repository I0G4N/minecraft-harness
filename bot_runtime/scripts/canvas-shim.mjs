// prismarine-viewer imports the optional `canvas` devDependency for name tags.
// Map it to the prebuilt @napi-rs/canvas so production images need no native build.
import { mkdirSync, writeFileSync } from 'node:fs'
const directory = new URL('../node_modules/canvas/', import.meta.url)
mkdirSync(directory, { recursive: true })
writeFileSync(new URL('package.json', directory), JSON.stringify({ name: 'canvas', version: '3.1.0' }) + '\n')
writeFileSync(new URL('index.js', directory), 'module.exports = require("@napi-rs/canvas")\n')
