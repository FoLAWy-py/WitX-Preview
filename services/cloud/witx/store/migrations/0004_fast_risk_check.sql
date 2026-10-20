-- An owner may let the risk check try OpenAI's Decisions API first.
ALTER TABLE tenants ADD COLUMN fast_risk_check boolean NOT NULL DEFAULT false;
GRANT UPDATE (fast_risk_check) ON tenants TO witx_app;
