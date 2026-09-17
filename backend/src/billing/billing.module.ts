import { Module } from "@nestjs/common";

import { AiModule } from "../ai/ai.module";
import { SecurityModule } from "../security/security.module";
import {
  BillingAdminController,
  ConsultationsController,
  ServicesController,
  WebhooksController,
} from "./billing.controller";
import { BillingService } from "./billing.service";
import { PaymobService } from "./paymob.service";
import { ServicesService } from "./services.service";

@Module({
  imports: [AiModule, SecurityModule],
  controllers: [ServicesController, ConsultationsController, WebhooksController, BillingAdminController],
  providers: [BillingService, PaymobService, ServicesService],
  exports: [BillingService, PaymobService, ServicesService],
})
export class BillingModule {}
