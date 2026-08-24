/**
 * HTTP surface for the product backend.
 *
 * Runs on :8001 beside the AI service on :8000. Separate processes, separate
 * database roles, separate languages — connected by exactly one thing: this
 * service publishes public keys, and that one verifies against them.
 */

import "dotenv/config";
import { Logger, ValidationPipe } from "@nestjs/common";
import { NestFactory } from "@nestjs/core";

import { AppModule } from "./app.module";
import { ErrorEnvelopeFilter } from "./common/errors";
import { loadConfig } from "./config/configuration";

async function bootstrap(): Promise<void> {
  const config = loadConfig();

  const app = await NestFactory.create(AppModule, {
    // Paymob computes its HMAC over the RAW request body. Body parsers throw
    // that away, so the raw buffer has to be preserved from the start —
    // retrofitting this after the webhook is written is the classic afternoon
    // lost to a signature that never matches.
    rawBody: true,
  });

  // One envelope for every status, matching the AI service. Nest's defaults
  // produce three different shapes for one API.
  app.useGlobalFilters(new ErrorEnvelopeFilter());

  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      forbidNonWhitelisted: true,
      transform: true,
    }),
  );

  app.enableCors({
    origin: new RegExp(config.corsOriginRegex),
    credentials: false,
    methods: ["GET", "POST", "DELETE", "OPTIONS"],
    allowedHeaders: ["Authorization", "Content-Type", "X-Admin-Key"],
  });

  await app.listen(config.port, "127.0.0.1");
  new Logger("bootstrap").log(
    `product backend on http://127.0.0.1:${config.port} — issuer ${config.jwtIssuer}`,
  );
}

bootstrap();
