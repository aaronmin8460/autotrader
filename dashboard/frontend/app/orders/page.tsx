"use client";

/**
 * Orders — the account-wide stream, unabridged.
 *
 * The same merged list the Overview shows eight rows of, at its full read
 * limit, with the per-store provenance badge on every row and the standing
 * statement that no shadow or simulated action is an input to it.
 *
 * The equity book's own intent log — requested against approved quantity, the
 * risk verdict, and the broker's status beside it — is on the Equity Paper
 * page, because it is that runtime's record rather than the account's.
 */

import Link from "next/link";

import { AccountOrders } from "@/components/AccountOrders";
import { PageHeader } from "@/components/shell/PageHeader";
import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";
import { useAccountOrders } from "@/lib/orders";

export default function OrdersPage() {
  const { t } = useI18n();
  const { account } = useDashboard();
  const { data: orders, loading } = useAccountOrders();

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("orders.title")}
        context={t("nav.detail.orders")}
        actions={
          <Link
            href="/equity-paper"
            className="rounded-xs px-2 py-1 text-meta font-medium text-accent hover:underline focus-visible:outline-2 focus-visible:outline-accent"
          >
            {t("orders.paperOrders")} · {t("nav.equityPaper")}
          </Link>
        }
      />

      <AccountOrders
        panel={orders}
        generatedAt={orders?.generated_at ?? account.data?.generated_at ?? null}
        loading={loading}
        title={t("orders.accountWide")}
      />
    </div>
  );
}
