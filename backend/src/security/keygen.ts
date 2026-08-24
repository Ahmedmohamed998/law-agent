/**
 * Create a signing key.
 *
 *   npm run keygen
 *
 * Nothing works before this: with no key the service can neither issue nor
 * verify, and /readyz says so.
 */

import "dotenv/config";

import { KeyService } from "./key.service";
import { loadConfig } from "../config/configuration";

async function main(): Promise<void> {
  const service = new KeyService();
  await service.reload();
  const before = service.all().size;

  const key = await service.generate();
  const config = loadConfig();

  console.log(`created ${key.path}`);
  console.log(`kid: ${key.kid}`);
  console.log();

  if (before > 0) {
    console.log(
      `${service.all().size} keys now on disk. JWKS publishes all of them, so`,
    );
    console.log("tokens signed by either still verify. To switch signing over:");
    console.log();
    console.log(`  ACTIVE_KID=${key.kid}`);
    console.log();
    console.log("Delete the old PEM only after every token it signed has");
    console.log(`expired (access TTL is ${config.accessTokenTtlSeconds}s).`);
  } else {
    console.log("This is the only key. Point the AI service at:");
    console.log(`  JWKS_URL=${config.jwtIssuer}/.well-known/jwks.json`);
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
