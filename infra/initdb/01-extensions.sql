-- =============================================================================
-- postmarkTrip · 数据库初始化扩展
--
-- ⚠️ 这个文件**只在数据目录为空时执行一次**。
-- PostgreSQL 官方镜像（以及基于它的 pgvector/pgvector）只在初始化新集群时运行
-- /docker-entrypoint-initdb.d 下的脚本。data/db 里已经有数据时，改动这里不会生效。
--
-- 因此本文件的原则是：**只做"必须有、且幂等"的事**，其余交给 Alembic 迁移。
-- 从旧备份恢复、或更换镜像后，请照 migrations/README.md 的补救章节手动补扩展。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. pg_trgm —— 必需
--
-- 为什么是 trgm 而不是 tsvector：PostgreSQL 自带的全文检索**不切中文**
-- （没有内置的中文分词器，整句会被当成一个词元），所以中文模糊搜索走
-- trigram 索引（gin_trgm_ops）才是可行路线。
--
-- db/models.py 为以下列建了 gin_trgm_ops 索引：
--   place.name / post_office.name / card.title / card.note
--   postmark.inscription / stamp.name
--
-- 没有这个扩展，初始迁移会在建索引时直接失败 —— 所以这里**失败即中止初始化**，
-- 早失败比"迁移跑到一半报一个看不懂的错"要好。
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_trgm';
        RAISE NOTICE '[initdb] pg_trgm 已启用（中文模糊搜索依赖它）';
    ELSE
        RAISE EXCEPTION
            '[initdb] pg_trgm 不可用。它属于 postgresql-contrib，官方 postgres 镜像自带。'
            '请确认使用的是标准镜像（本项目默认 pgvector/pgvector:pg16）。';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- 2. uuid-ossp —— 可选
--
-- 主键 UUID **由应用层生成**（db/ids.py 生成有序的 UUID v7，default=uuid7），
-- 不依赖数据库函数。所以这个扩展对功能没有任何影响，装它只是为了让
-- 运维在 psql 里手写查询时能直接用 uuid_generate_v4()。
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'uuid-ossp') THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"';
            RAISE NOTICE '[initdb] uuid-ossp 已启用（可选，仅供手工查询使用）';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE '[initdb] uuid-ossp 跳过：%', SQLERRM;
        END;
    ELSE
        RAISE NOTICE '[initdb] uuid-ossp 不可用，跳过（不影响功能）';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- 3. vector (pgvector) —— 可选，v1.x 才用
--
-- 默认配置**不依赖** pgvector：v0.1 完全没有向量检索，以图搜戳/搜票是 v1.2 的事。
-- 默认镜像恰好带了它，那就顺手启用；换成普通 postgres 镜像时这里会安静跳过，
-- 系统依旧完整可用（D5：一个 Provider 都不填也能跑）。
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS vector';
            RAISE NOTICE '[initdb] pgvector 已启用（v1.x 的向量检索将使用它）';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE '[initdb] pgvector 跳过：%', SQLERRM;
        END;
    ELSE
        RAISE NOTICE '[initdb] pgvector 不可用，跳过（v0.1/v1.0 不需要它）';
    END IF;
END
$$;


-- -----------------------------------------------------------------------------
-- 4. 会话时区
--
-- TIMESTAMPTZ 内部恒为 UTC，这里只影响**显示**：把库级默认时区设为
-- Asia/Shanghai，运维在 psql 里看到的 created_at 就是本地时间，不必手算。
-- 应用侧仍然全部使用带时区的 datetime（见 db/base.py）。
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'Asia/Shanghai');
    RAISE NOTICE '[initdb] 数据库默认时区设为 Asia/Shanghai';
END
$$;
