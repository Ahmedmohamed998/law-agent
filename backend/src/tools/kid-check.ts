/**
 * Does Node derive the same `kid` from a PEM that Python does?
 *
 * The whole migration rests on this. If the thumbprints differ, every token
 * signed by the Node service names a key id the AI service cannot find in
 * JWKS, and every request fails with an unhelpful 401. Both sides implement
 * RFC 7638 over the same canonical JSON, so they should agree — this proves it
 * against the actual key file rather than assuming.
 *
 *   npx tsx src/tools/kid-check.ts
 */

import "dotenv/config";

import { basename } from "node:path";

import { KeyService } from "../security/key.service";

async function main(): Promise<void> {
  const service = new KeyService();
  await service.reload();

  const keys = service.all();
  console.log(`keys loaded: ${keys.size}`);

  let allMatch = keys.size > 0;
  for (const [kid, key] of keys) {
    // Python named the file after ITS computed thumbprint, so the filename is
    // Python's answer and `kid` is Node's. Equal filename means equal answer.
    const fromFilename = basename(key.path, ".pem");
    const match = kid === fromFilename;
    allMatch &&= match;
    console.log(`  kid computed by Node : ${kid}`);
    console.log(`  kid computed by Py   : ${fromFilename}`);
    console.log(`  MATCH                : ${match}`);
  }

  const doc = await service.jwks();
  console.log("\njwks entry:");
  console.log(JSON.stringify(doc.keys[0], null, 1));

  const leaked = ["d", "p", "q", "dp", "dq", "qi"].filter(
    (m) => Object.hasOwn(doc.keys[0] as object, m),
  );
  console.log("\nprivate material in jwks:", leaked.length ? leaked : "none");

  if (!allMatch || leaked.length) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
