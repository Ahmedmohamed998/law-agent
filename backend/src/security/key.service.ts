/**
 * Signing keys and the JWKS document.
 *
 * This service is the only holder of a private key in the system. The AI
 * service fetches the public half over `/.well-known/jwks.json` and verifies;
 * it can never mint. A shared symmetric secret was rejected deliberately — it
 * would let the AI service issue tokens for the identity domain, and would
 * make rotation a coordinated deploy of two services instead of a file drop in
 * one.
 *
 * Keys are PEM files in `keysDir`, named by their `kid`. The `kid` is the
 * RFC 7638 JWK thumbprint, derived from the key material itself, so the same
 * key always gets the same id and two keys can never collide.
 *
 * Compatibility note: `calculateJwkThumbprint` computes the identical value
 * that Python's `product/security/keys.py` computes, because both implement
 * RFC 7638 over the same canonical JSON. The PEM files written by the Python
 * service therefore keep their ids here — the AI service cannot tell which
 * implementation signed a token, which is what makes the migration a swap
 * rather than a re-issue of every credential in circulation.
 *
 * Rotation:
 *   1. `npm run keygen` writes a second PEM.
 *   2. JWKS publishes BOTH. Verifiers pick by `kid`, so either validates.
 *   3. Set ACTIVE_KID to the new kid and restart.
 *   4. Delete the old PEM once its last token has expired.
 *
 * Step 2 is the one people skip. Pull the old public key early and every
 * request in flight fails at once — staggered by an hour of verifier caching,
 * so it reads as an intermittent bug rather than a rotation mistake.
 */

import { Injectable, Logger, OnModuleInit } from "@nestjs/common";
import { chmod, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { generateKeyPair } from "node:crypto";
import { join } from "node:path";
import { promisify } from "node:util";
import {
  calculateJwkThumbprint,
  exportJWK,
  importPKCS8,
  type JWK,
  type KeyLike,
} from "jose";

import { loadConfig } from "../config/configuration";

const generateKeyPairAsync = promisify(generateKeyPair);

export const KEY_ALGORITHM = "RS256";
const KEY_MODULUS_LENGTH = 2048;
const PEM_SUFFIX = ".pem";

export class NoSigningKeyError extends Error {}

export interface SigningKey {
  kid: string;
  privateKey: KeyLike;
  jwk: JWK;
  path: string;
  mtimeMs: number;
}

@Injectable()
export class KeyService implements OnModuleInit {
  private static readonly log = new Logger(KeyService.name);
  private keys = new Map<string, SigningKey>();
  private readonly config = loadConfig();

  async onModuleInit(): Promise<void> {
    await this.reload();
    try {
      const active = await this.active();
      KeyService.log.log(`signing key ready, kid=${active.kid}`);
    } catch (err) {
      // Not fatal at boot: health checks should still answer, and a loud log
      // plus a failing /readyz is more useful than a crash loop that hides a
      // one-line fix.
      KeyService.log.error((err as Error).message);
    }
  }

  /** Re-read keysDir. Call after writing a new key. */
  async reload(): Promise<void> {
    const loaded = new Map<string, SigningKey>();
    let entries: string[];
    try {
      entries = await readdir(this.config.keysDir);
    } catch {
      this.keys = loaded;
      return;
    }

    for (const name of entries.sort()) {
      if (!name.endsWith(PEM_SUFFIX)) continue;
      const path = join(this.config.keysDir, name);
      try {
        const pem = await readFile(path, "utf8");
        const privateKey = await importPKCS8(pem, KEY_ALGORITHM, {
          extractable: true,
        });
        const jwk = await exportJWK(privateKey);
        const kid = await calculateJwkThumbprint(jwk, "sha256");
        const { mtimeMs } = await import("node:fs").then((fs) =>
          fs.promises.stat(path),
        );
        loaded.set(kid, { kid, privateKey, jwk, path, mtimeMs });
      } catch {
        // A malformed file must not take the service down — the other keys
        // still verify tokens already in circulation.
        KeyService.log.warn(`skipping unreadable key file: ${name}`);
      }
    }
    this.keys = loaded;
  }

  /** Every key on disk, by kid. */
  all(): Map<string, SigningKey> {
    return this.keys;
  }

  byKid(kid: string): SigningKey | undefined {
    return this.keys.get(kid);
  }

  /**
   * The key that signs new tokens.
   *
   * A configured kid that is missing is a hard error, never a silent
   * fallback: quietly signing with a different key than operations intended
   * is exactly the confusion a rotation must not create.
   */
  async active(): Promise<SigningKey> {
    if (this.keys.size === 0) {
      await this.reload();
    }
    if (this.keys.size === 0) {
      throw new NoSigningKeyError(
        `no signing key in ${this.config.keysDir} — run: npm run keygen`,
      );
    }

    const configured = this.config.activeKid;
    if (configured) {
      const key = this.keys.get(configured);
      if (!key) {
        throw new NoSigningKeyError(
          `ACTIVE_KID=${configured} is not in ${this.config.keysDir}`,
        );
      }
      return key;
    }

    // Unset: newest file wins. Fine for development, which is why production
    // is expected to pin it.
    return [...this.keys.values()].reduce((a, b) =>
      b.mtimeMs > a.mtimeMs ? b : a,
    );
  }

  /**
   * The public document. Every key, so a rotation verifies both ways.
   *
   * `exportJWK` on a private key yields the private components too, so they
   * are stripped explicitly here. Publishing `d` would hand every verifier the
   * ability to mint — the single failure that collapses the whole design.
   */
  async jwks(): Promise<{ keys: JWK[] }> {
    const out: JWK[] = [];
    for (const key of this.keys.values()) {
      const { kty, n, e } = key.jwk;
      out.push({ kty, n, e, use: "sig", alg: KEY_ALGORITHM, kid: key.kid });
    }
    return { keys: out };
  }

  /** Create a new keypair on disk and return it. */
  async generate(): Promise<SigningKey> {
    await mkdir(this.config.keysDir, { recursive: true });

    const { privateKey } = await generateKeyPairAsync("rsa", {
      modulusLength: KEY_MODULUS_LENGTH,
      publicKeyEncoding: { type: "spki", format: "pem" },
      // Unencrypted PKCS8, matching the Python service: the file itself is the
      // secret, and a passphrase this process would hold in its environment
      // anyway is ceremony rather than protection. Protect it with file
      // permissions and by keeping keysDir out of the image and out of git.
      privateKeyEncoding: { type: "pkcs8", format: "pem" },
    });

    const imported = await importPKCS8(privateKey as string, KEY_ALGORITHM, {
      extractable: true,
    });
    const kid = await calculateJwkThumbprint(await exportJWK(imported), "sha256");
    const path = join(this.config.keysDir, `${kid}${PEM_SUFFIX}`);

    await writeFile(path, privateKey as string, { mode: 0o600 });
    try {
      await chmod(path, 0o600); // no-op on Windows, correct everywhere else
    } catch {
      /* ignore */
    }

    await this.reload();
    const created = this.keys.get(kid);
    if (!created) {
      throw new NoSigningKeyError(`wrote ${path} but could not read it back`);
    }
    return created;
  }
}
