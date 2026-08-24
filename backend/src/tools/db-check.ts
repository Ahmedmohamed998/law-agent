/**
 * Proves the Node service sees the same database the Python one migrated.
 *
 *   npx tsx src/tools/db-check.ts
 */

import "dotenv/config";

import { PrismaPg } from "@prisma/adapter-pg";

import { PrismaClient } from "../../generated/prisma/client";

async function main(): Promise<void> {
  const prisma = new PrismaClient({
    adapter: new PrismaPg({ connectionString: process.env.DATABASE_URL }),
  });

  const counts = {
    users: await prisma.users.count(),
    organizations: await prisma.organizations.count(),
    memberships: await prisma.memberships.count(),
    refresh_tokens: await prisma.refresh_tokens.count(),
    erasure_requests: await prisma.erasure_requests.count(),
  };
  console.log("tables readable via Prisma:", counts);

  // The boundary, from this side. The application role has no grant on `ai`,
  // and that must hold for Prisma exactly as it holds for SQLAlchemy.
  try {
    await prisma.$queryRawUnsafe("select count(*) from ai.sessions");
    console.log("BOUNDARY FAIL: product role can read ai.sessions");
    process.exit(1);
  } catch (err) {
    const msg = (err as Error).message.split("\n")[0];
    console.log("boundary holds: ai.sessions blocked —", msg.slice(0, 80));
  }

  await prisma.$disconnect();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
