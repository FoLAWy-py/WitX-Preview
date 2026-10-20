-- The model providers a tenant's screenshots and text may be sent to .
-- NULL until an owner chooses: then only the provider of the deployment's default model.
ALTER TABLE tenants ADD COLUMN allowed_providers text[]
  CHECK (allowed_providers IS NULL OR cardinality(allowed_providers) > 0);
GRANT UPDATE (allowed_providers) ON tenants TO witx_app;
