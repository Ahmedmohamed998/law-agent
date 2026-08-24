import { Module } from "@nestjs/common";

import { SecurityModule } from "../security/security.module";
import { WellKnownController } from "./wellknown.controller";

@Module({
  imports: [SecurityModule],
  controllers: [WellKnownController],
})
export class WellKnownModule {}
