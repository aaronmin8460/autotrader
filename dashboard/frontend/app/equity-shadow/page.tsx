import { redirect } from "next/navigation";

/**
 * The old Equity Shadow route. Kept so bookmarks and the shadow deployment's
 * documentation keep working; the record now lives in the Shadows workspace
 * beside the A1-B observer.
 */
export default function EquityShadowRedirect() {
  redirect("/shadows");
}
