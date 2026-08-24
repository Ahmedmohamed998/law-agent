/**
 * Password hashing.
 *
 * Argon2id, with parameters pinned to what the Python service used rather than
 * left at this library's defaults. That is not cosmetic:
 *
 *   argon2-cffi default : m=65536, t=3, p=4
 *   @node-rs/argon2     : m=19456, t=2, p=1
 *
 * Both libraries verify each other's hashes — the parameters live in the PHC
 * string, and this was checked against a real hash from each side. But if this
 * service hashed at the weaker defaults, `needsRehash` would report every
 * migrated user's hash as stale and quietly re-hash it *down* to 19MiB of
 * memory on their next login. A silent security downgrade performed by the
 * upgrade path is worse than no upgrade path.
 *
 * Two behaviours matter as much as the hashing:
 *
 *   * `verify` never throws to the caller. A wrong password, a corrupt hash,
 *     and an unparseable stored value all mean the same thing at the API
 *     boundary — the credential is not good — and letting them differ leaks
 *     which case a given account is in.
 *
 *   * `dummyVerify` exists so logging in with an unknown email costs the same
 *     as logging in with a known one. Without it, response time answers "does
 *     this address have an account?" for anyone willing to measure, which is a
 *     real disclosure for a law firm's client list.
 */

import { Injectable } from "@nestjs/common";
import { Algorithm, hash, verify, type Options } from "@node-rs/argon2";

const PARAMS: Options = {
  algorithm: Algorithm.Argon2id,
  memoryCost: 65536,
  timeCost: 3,
  parallelism: 4,
};

@Injectable()
export class PasswordService {
  /** Computed once, lazily, and reused to burn time on the unknown-user path. */
  private dummyHash: string | null = null;

  async hash(plaintext: string): Promise<string> {
    return hash(plaintext, PARAMS);
  }

  /** True if the password matches. Never throws. */
  async verify(storedHash: string | null, plaintext: string): Promise<boolean> {
    if (!storedHash) {
      // An anonymous user has no password. Still spend the time.
      await this.dummyVerify();
      return false;
    }
    try {
      return await verify(storedHash, plaintext, PARAMS);
    } catch {
      return false;
    }
  }

  /** Spend the same time as a real verification, for an account that does not
   * exist. */
  async dummyVerify(): Promise<void> {
    try {
      this.dummyHash ??= await hash("timing-equalisation-placeholder", PARAMS);
      await verify(this.dummyHash, "not-the-password", PARAMS);
    } catch {
      /* the point is the elapsed time, not the answer */
    }
  }

  /**
   * True when the stored hash was made with weaker parameters than current
   * policy.
   *
   * Called after a successful verify — the only moment the plaintext is in
   * hand to re-hash with, and therefore the only chance to upgrade an old hash
   * without asking the user to change their password.
   */
  needsRehash(storedHash: string): boolean {
    // The PHC string is $argon2id$v=19$m=65536,t=3,p=4$salt$hash
    const match = /^\$argon2id\$v=19\$m=(\d+),t=(\d+),p=(\d+)\$/.exec(storedHash);
    if (!match) return true; // not argon2id at current version — replace it
    const [, m, t, p] = match;
    return (
      Number(m) < PARAMS.memoryCost! ||
      Number(t) < PARAMS.timeCost! ||
      Number(p) < PARAMS.parallelism!
    );
  }
}
