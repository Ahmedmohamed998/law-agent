import { Module } from "@nestjs/common";

import { SecurityModule } from "../security/security.module";
import { HealthController } from "./health.controller";

@Module({
  imports: [SecurityModule],
  controllers: [HealthController],
})
export class HealthModule {}
