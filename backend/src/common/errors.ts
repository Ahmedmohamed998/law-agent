/**
 * One error envelope for everything, matching the AI service:
 *
 *   { "error": { "code": "...", "message": "..." } }
 *
 * Nest's default is `{ "statusCode", "message", "error" }` and its validation
 * pipe returns an array — three shapes for one API. The AI service had exactly
 * this drift and it meant a client branching on `error.code` silently read
 * undefined. One filter, every status.
 */

import {
  ArgumentsHost,
  Catch,
  ExceptionFilter,
  HttpException,
  HttpStatus,
  Logger,
} from "@nestjs/common";
import type { Response } from "express";

export class AppError extends HttpException {
  constructor(status: number, code: string, message: string) {
    super({ code, message }, status);
  }
}

export const unauthorized = (message: string) =>
  new AppError(HttpStatus.UNAUTHORIZED, "unauthenticated", message);

export const forbidden = (message: string) =>
  new AppError(HttpStatus.FORBIDDEN, "forbidden", message);

export const notFound = (code: string, message: string) =>
  new AppError(HttpStatus.NOT_FOUND, code, message);

export const conflict = (code: string, message: string) =>
  new AppError(HttpStatus.CONFLICT, code, message);

@Catch()
export class ErrorEnvelopeFilter implements ExceptionFilter {
  private static readonly log = new Logger("error");

  catch(exception: unknown, host: ArgumentsHost): void {
    const response = host.switchToHttp().getResponse<Response>();

    if (exception instanceof HttpException) {
      const status = exception.getStatus();
      const body = exception.getResponse();

      if (typeof body === "object" && body !== null && "code" in body) {
        const { code, message } = body as { code: string; message?: string };
        response.status(status).json({ error: { code, message: message ?? "" } });
        return;
      }

      // ValidationPipe and Nest's built-in exceptions land here. Its `message`
      // is an array of every failing constraint; the first one is what a
      // developer needs, and the rest is noise in a body meant to be read.
      const detail = body as { message?: string | string[] };
      const raw = Array.isArray(detail?.message) ? detail.message[0] : detail?.message;
      response.status(status).json({
        error: {
          code: status === HttpStatus.BAD_REQUEST ? "invalid_request" : "error",
          message: raw ?? exception.message,
        },
      });
      return;
    }

    // Anything unexpected. Log the real thing, tell the caller nothing — a
    // stack trace in a response body is a gift to whoever is probing.
    ErrorEnvelopeFilter.log.error(exception);
    response.status(HttpStatus.INTERNAL_SERVER_ERROR).json({
      error: { code: "internal_error", message: exception instanceof Error ? exception.message : "unexpected failure" },
    });
  }
}
