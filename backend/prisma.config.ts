// Prisma 7 moved the connection URL out of schema.prisma and into this file.
//
// The distinction it forces is one this project already made deliberately:
// the CLI (introspect, migrate) connects as the OWNER, while the running
// application connects as a role that owns nothing and therefore cannot alter
// its own tables. Two URLs, and the split is the point — see
// product/sql/bootstrap_product.sql.

import "dotenv/config";
import { defineConfig, env } from "prisma/config";

export default defineConfig({
  schema: "prisma/schema.prisma",
  datasource: {
    // Owner role: introspection reads catalogs and migrations write DDL.
    // Never the application role.
    url: env("DATABASE_MIGRATE_URL"),
  },
});
