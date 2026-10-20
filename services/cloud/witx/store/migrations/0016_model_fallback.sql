-- An owner may let a task switch to another allowed model when its model keeps failing
-- with errors a provider may recover from . Off until an owner turns it on.
ALTER TABLE tenants ADD COLUMN model_fallback boolean NOT NULL DEFAULT false;
GRANT UPDATE (model_fallback) ON tenants TO witx_app;
