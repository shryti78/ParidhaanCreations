import React, { useEffect, useState } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { Search, Bell } from "lucide-react";
import axios from "axios";
import { API } from "@/App";
import { useLanguage } from "@/contexts/LanguageContext";
import { useSiteSettings } from "@/contexts/SiteSettingsContext";

const DEFAULT_LOGO = "/logo-header.png";

/**
 * Mobile-only sticky header matching the approved redesign at
 *   https://claude.ai/artifact/SALgDi6dDRwbVRjehoLXqg
 *
 * Shown only on viewports < md (768px). On desktop the existing Navbar keeps
 * its full white top bar. The order of elements, top to bottom:
 *
 *   1. Compact row:  logo (left) · language toggle + notification bell (right)
 *   2. Search field: tappable pill that routes to /products?search=<q>
 *   3. Category chips: horizontal scroll, backend-driven, tap to filter
 *
 * Background is the brand maroon — it anchors the page and lets the chips
 * and search float as cream cards above it. Highlighted category reflects
 * the current URL (?category=slug) so the header stays in sync when users
 * navigate via the chips.
 */

// Fallback chips used only if /api/categories returns empty (first-run,
// or before the admin has seeded any). Covers the categories that actually
// exist on the site today.
const FALLBACK_CATEGORIES = [
  { category_id: "handicrafts", slug: "handicrafts", name: "Handicrafts" },
  { category_id: "pooja", slug: "pooja", name: "Pooja Articles" },
  { category_id: "laddu-gopal", slug: "laddu-gopal", name: "Laddu Gopal" },
  { category_id: "dry-fruits", slug: "dry-fruits", name: "Dry Fruits" },
];

export default function MobileHeader() {
  const navigate = useNavigate();
  const location = useLocation();
  const { settings, getLogoUrl } = useSiteSettings();
  const { language, toggleLanguage } = useLanguage();
  const headerLogo = getLogoUrl(settings?.header_logo) || DEFAULT_LOGO;

  const [categories, setCategories] = useState(FALLBACK_CATEGORIES);
  const [searchQuery, setSearchQuery] = useState("");
  const [notifCount, setNotifCount] = useState(0);

  // Determine the active category from URL so the chip highlights correctly
  // when the user arrived via /products?category=pooja or navigated anywhere.
  const urlParams = new URLSearchParams(location.search);
  const activeSlug = urlParams.get("category") || "";
  const onProductsListing = location.pathname === "/products";

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await axios.get(`${API}/categories`);
        if (cancelled) return;
        // Only keep active parent categories; sort by display_order as the
        // backend already does, but defend against unexpected missing fields.
        const list = Array.isArray(res.data) ? res.data : [];
        if (list.length > 0) setCategories(list);
      } catch {
        // Keep fallback — never let the header break on an offline backend
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Pending notifications badge (orders status updates, etc).
  // Soft-fails to 0 — this is cosmetic, not core.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await axios.get(`${API}/notifications/unread-count`, { withCredentials: true });
        if (!cancelled) setNotifCount(res.data?.count || 0);
      } catch {
        if (!cancelled) setNotifCount(0);
      }
    })();
  }, [location.pathname]);

  const handleSearchSubmit = (e) => {
    e.preventDefault();
    const q = searchQuery.trim();
    if (q) {
      navigate(`/products?search=${encodeURIComponent(q)}`);
    } else {
      navigate("/products");
    }
  };

  return (
    <header
      className="md:hidden sticky top-0 z-40 bg-primary text-white shadow-sm"
      role="banner"
      aria-label="Mobile site header"
    >
      {/* Row 1: logo + language + notifications */}
      <div className="flex items-center justify-between px-3 pt-2 pb-2">
        <Link to="/" aria-label="Paridhaan Creations home" className="flex items-center">
          <img
            src={headerLogo}
            alt={settings?.site_name || "Paridhaan Creations"}
            className="h-10 w-auto object-contain"
            style={{ backgroundColor: "transparent" }}
          />
        </Link>

        <div className="flex items-center gap-2">
          {/* Compact language toggle designed for the maroon header. Shows
              a flag + ISO code; tap to switch between EN and HI (handled by
              the shared LanguageContext so it stays in sync everywhere). */}
          <button
            type="button"
            onClick={toggleLanguage}
            aria-label={language === "en" ? "Switch to Hindi" : "Switch to English"}
            className="flex items-center gap-1.5 bg-white/15 hover:bg-white/25 transition-colors rounded-full px-2.5 py-1.5 text-xs font-semibold text-white"
            data-testid="mobile-header-language"
          >
            <span aria-hidden="true">{language === "en" ? "🇮🇳" : "🇬🇧"}</span>
            <span>{language === "en" ? "IN" : "EN"}</span>
          </button>

          <Link
            to="/my-orders"
            aria-label={notifCount ? `${notifCount} new notifications` : "Notifications"}
            className="relative w-9 h-9 rounded-full bg-white/15 flex items-center justify-center hover:bg-white/25 transition-colors"
            data-testid="mobile-header-notifications"
          >
            <Bell className="w-4 h-4" aria-hidden="true" />
            {notifCount > 0 && (
              <span
                className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-secondary text-primary text-[10px] font-bold flex items-center justify-center border border-primary"
                aria-hidden="true"
              >
                {notifCount > 9 ? "9+" : notifCount}
              </span>
            )}
          </Link>
        </div>
      </div>

      {/* Row 2: search */}
      <form onSubmit={handleSearchSubmit} className="px-3 pb-2" role="search">
        <label htmlFor="mobile-search" className="sr-only">Search products</label>
        <div className="relative">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
            aria-hidden="true"
          />
          <input
            id="mobile-search"
            type="search"
            inputMode="search"
            enterKeyHint="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={'Search "Laddu Gopal dress"...'}
            className="w-full pl-9 pr-3 py-2.5 rounded-lg bg-white text-sm text-gray-800 placeholder:text-gray-400 border-none focus:outline-none focus:ring-2 focus:ring-secondary"
            data-testid="mobile-header-search"
          />
        </div>
      </form>

      {/* Row 3: category chips. Live on a cream sub-strip so the maroon
          header reads as one block and chips look like a shelf attached. */}
      <div className="bg-background border-t border-primary-dark overflow-x-auto scrollbar-hide" role="navigation" aria-label="Product categories">
        <div className="flex items-center gap-2 px-3 py-2 whitespace-nowrap">
          <Link
            to="/products"
            aria-current={onProductsListing && !activeSlug ? "page" : undefined}
            data-testid="chip-all"
            className={`inline-flex items-center px-3 py-1.5 rounded-full text-xs font-semibold transition-colors ${
              onProductsListing && !activeSlug
                ? "bg-primary text-white"
                : "bg-white text-primary border border-gray-200"
            }`}
          >
            All
          </Link>
          {categories.map((cat) => {
            const slug = cat.slug || cat.category_id;
            const name = cat.name || cat.title || slug;
            const isActive = onProductsListing && activeSlug === slug;
            return (
              <Link
                key={slug}
                to={`/products?category=${encodeURIComponent(slug)}`}
                aria-current={isActive ? "page" : undefined}
                data-testid={`chip-${slug}`}
                className={`inline-flex items-center px-3 py-1.5 rounded-full text-xs font-semibold transition-colors ${
                  isActive
                    ? "bg-primary text-white"
                    : "bg-white text-primary border border-gray-200"
                }`}
              >
                {name}
              </Link>
            );
          })}
        </div>
      </div>
    </header>
  );
}
