import { useState, type FormEvent } from "react";
import {
  Navigate,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router";
import { Languages } from "lucide-react";
import { LOCALES, useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { useAsync } from "@/lib/useAsync";
import { Logo } from "@/components/Logo";
import { Button, Field, Spinner, inputClass } from "@/components/ui/base";
import { Select } from "@/components/ui/select";

const PROVIDER_ERRORS: Record<string, MessageKey> = {
  not_completed: "signin.error.notCompleted",
  not_invited: "signin.error.notInvited",
};

export default function SignIn() {
  const { t, locale, setLocale } = useI18n();
  const { me, api, refresh } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const returnTo = (location.state as { from?: string } | null)?.from ?? "/";
  const methods = useAsync(() => api.authMethods(), [api]);
  const [value, setValue] = useState("");
  const providerError = PROVIDER_ERRORS[params.get("error") ?? ""];
  const [error, setError] = useState<string | undefined>(
    providerError ? t(providerError) : undefined,
  );
  const [busy, setBusy] = useState(false);

  if (me) return <Navigate to={returnTo} replace />;

  async function withToken(event: FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.devLogin(value.trim());
      await refresh();
      navigate(returnTo, { replace: true });
    } catch {
      setError(t("signin.devToken.error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-[380px]">
        <div className="mb-8 flex justify-center">
          <Logo />
        </div>
        <div className="rounded-2xl border border-line bg-surface p-6">
          <h1 className="text-[20px] font-semibold tracking-tight">
            {t("signin.title")}
          </h1>
          <p className="mt-1 mb-6 text-[14px] text-muted">
            {t("signin.subtitle")}
          </p>
          <div className="space-y-4">
            {error && (
              <p
                role="alert"
                className="rounded-lg bg-red-soft px-3 py-2 text-[13.5px] text-red"
              >
                {error}
              </p>
            )}
            {!methods.data && <Spinner className="py-4" />}
            {methods.data?.oidc && (
              <Button
                variant="primary"
                size="lg"
                className="w-full"
                onClick={() => window.location.assign(api.loginUrl(returnTo))}
              >
                {t("signin.oidc")}
              </Button>
            )}
            {methods.data?.dev && (
              <form onSubmit={withToken} className="space-y-3">
                <Field
                  htmlFor="token"
                  label={t("signin.devToken")}
                  hint={t("signin.devToken.help")}
                >
                  <input
                    id="token"
                    type="password"
                    autoComplete="off"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    className={inputClass}
                  />
                </Field>
                <Button
                  type="submit"
                  size="lg"
                  className="w-full"
                  busy={busy}
                  variant={methods.data.oidc ? "secondary" : "primary"}
                >
                  {t("signin.devToken.submit")}
                </Button>
              </form>
            )}
          </div>
        </div>
        <div className="mt-6 flex justify-center">
          <Select
            label={t("account.language")}
            icon={Languages}
            value={locale}
            onChange={setLocale}
            placement="top-start"
            minWidth={180}
            options={LOCALES.map((l) => ({ value: l.id, label: l.label }))}
          />
        </div>
      </div>
    </div>
  );
}
