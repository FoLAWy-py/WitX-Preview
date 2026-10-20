import type { ActionView } from "@/api/types.gen";
import type { Locale, MessageKey } from "@/i18n";
import { money } from "./format";

type Effect = ActionView["effects"][number];

/** What an effect chip names besides the effect: the amount paid or the person
 * messaged when the terms say, otherwise what the agent said the input changes. Null
 * when neither is known: the chip then names the effect alone. */
export function effectObject(
  effect: Effect,
  action: Pick<ActionView, "changes" | "intent">,
  locale: Locale,
): string | null {
  const terms = action.intent ?? {};
  if ((effect === "payment" || effect === "purchase") && terms.amount)
    return money(terms.amount, terms.currency ?? "", locale);
  if (effect === "send_message" && terms.recipient) return terms.recipient;
  return action.changes || null;
}

/** What an input is called: typing that empties the field first is a replacement. */
export function kindKey(
  action: Pick<ActionView, "kind" | "input">,
): MessageKey {
  if (action.kind === "type_text" && action.input.replace === true)
    return "kind.type_text_replace";
  return `kind.${action.kind}` as MessageKey;
}
