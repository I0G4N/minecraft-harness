// Programming surface, not the security boundary. Filesystem/process/network
// enforcement belongs to the separate native-filtered execution container.
export const GAME_MODULES = Object.freeze(['vec3', 'minecraft-data', 'mineflayer-pathfinder']);
export const PAPER_GAME_PACKAGES = Object.freeze(['prismarine-item', 'prismarine-block', 'prismarine-entity', 'prismarine-windows', 'minecraft-protocol']);
// Paper §2.1 allows Node built-ins but not process creation or file writes.
// The native read-only sandbox/network namespace enforces those limits; this
// list only removes module-loading friction, it is not a security boundary.
export const PAPER_NODE_MODULES = Object.freeze(['assert', 'buffer', 'crypto', 'events', 'fs', 'fs/promises', 'os', 'path', 'querystring', 'stream', 'string_decoder', 'timers', 'timers/promises', 'tty', 'url', 'util', 'zlib']);
export function modulePolicy(lifecycle) {
  const generic = lifecycle === 'paper_aligned' ? PAPER_NODE_MODULES.flatMap(name => [name, `node:${name}`]) : [];
  return { schema_version: 1, policy_kind: 'required_external_boundary', allowed: [...GAME_MODULES, ...(lifecycle === 'paper_aligned' ? ['mineflayer', ...PAPER_GAME_PACKAGES] : []), ...generic], network: 'requires fixed identity proxy in isolated Minecraft deployment', filesystem_write: 'requires native deny-write filter and read-only container', process_creation: 'requires native deny-exec/process filter', vm_is_security_boundary: false };
}
