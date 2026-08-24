import { Module } from "@nestjs/common";

import { AiModule } from "../ai/ai.module";
import { SecurityModule } from "../security/security.module";
import {
  BillingAdminController,
  ConsultationsController,
  WebhooksController,
} from "./billing.controller";
import { BillingService } from "./billing.service";
import { PaymobService } from "./paymob.service";

@Module({
  imports: [AiModule, SecurityModule],
  controllers: [ConsultationsController, WebhooksController, BillingAdminController],
  providers: [BillingService, PaymobService],
  exports: [BillingService, PaymobService],
})
export class BillingModule {}
