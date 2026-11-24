import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Home, ShoppingBag, Sparkles, ShoppingCart, User } from "lucide-react";
import axios from "axios";
import { API } from "@/App";

/**
 * Mobile-only floating bottom navigation.
 *
 * Shown only on viewports < md (768px). On desktop the top Navbar handles
 * navigation. 5 items — Home, Shop, Astro (centered as hero), Cart (with
 * live count badge), Account. Replaces the hamburger menu as the primary
 * mobile nav pattern, matching the Flipkart-style pattern the user asked
 * for and the approved mockup at
 *   https://claude.ai/artifact/SALgDi6dDRwbVRjehoLXqg
 *
 * Cart badge count is kept in sync by:
 *   - Fetching on mount + on every route change
 *   - Listening for the window "cart-updated" event dispatched by cart
 *     add / remove helpers elsewhere in the app
 *
 * The nav is intentionally mounted globally in App.js (not nested under a
 * page) so its state survives route transitions and the bar can animate in
 * from a single owner.
 */
export default function MobileBottomNav() {
  const location = useLocation();
  const [cartCount, setCartCount] = useState(0);
  const [user, setUser] = useState(null);

  // Tabs in order. `match` decides which tab is highlighted for a given
  // pathname — some pages (checkout, order-success) extend the cart flow.
  const tabs = [
    { key: "home", to: "/", label: "Home", Icon: Home,
      match: (p) => p === "/" },
    { key: "shop", to: "/products", label: "Shop", Icon: ShoppingBag,
      match: (p) => p === "/products" || p.startsWith("/products/") || p.startsWith("/collection/") },
    { key: "astro", to: "/astro", label: "Astro", Icon: Sparkles, center: true,
      match: (p) => p.startsWith("/astro") },
    { key: "cart", to: "/cart", label: "Cart", Icon: ShoppingCart, badge: cartCount,
      match: (p) => p === "/cart" || p === "/checkout" || p.startsWith("/order-success") },
    { key: "account", to: user ? "/my-orders" : "/login", label: "Account", Icon: User,
      match: (p) => ["/login", "/my-orders", "/wishlist"].some((prefix) => p.startsWith(prefix)) ||
                    p.startsWith("/track/") },
  ];

  // Fetch cart count on mount + route change. Also pick up user so the
  // Account tab points to /my-orders when logged in and /login otherwise.
  useEffect(() => {
    let cancelled = false;

    const fetchCart = async () => {
      try {
        const res = await axios.get(`${API}/cart`, { withCredentials: true });
        if (cancelled) return;
        const count = res.data?.items?.reduce((sum, it) => sum + (it.quantity || 0), 0) || 0;
        setCartCount(count);
      } catch {
        // Cart may 401 for logged-out users — quiet fallback to 0
        if (!cancelled) setCartCount(0);
      }
    };

    const fetchUser = async () => {
      try {
        const res = await axios.get(`${API}/auth/me`, { withCredentials: true });
        if (!cancelled) setUser(res.data);
      } catch {
        if (!cancelled) setUser(null);
      }
    };

    fetchCart();
    fetchUser();
    return () => { cancelled = true; };
  }, [location.pathname]);

  // External hook: other components can dispatch
  //   window.dispatchEvent(new Event("cart-updated"))
  // after adding/removing an item to refresh the badge immediately without
  // waiting for the next navigation.
  useEffect(() => {
    const onCartUpdate = async () => {
      try {
        const res = await axios.get(`${API}/cart`, { withCredentials: true });
        const count = res.data?.items?.reduce((sum, it) => sum + (it.quantity || 0), 0) || 0;
        setCartCount(count);
      } catch { /* ignore */ }
    };
    window.addEventListener("cart-updated", onCartUpdate);
    return () => window.removeEventListener("cart-updated", onCartUpdate);
  }, []);

  return (
    <nav
      className="md:hidden fixed left-3 right-3 bottom-3 z-40 bg-white rounded-2xl border border-gray-200 shadow-[0_8px_24px_rgba(0,0,0,0.12)] px-1 py-2"
      role="navigation"
      aria-label="Mobile primary navigation"
      style={{ paddingBottom: "calc(0.5rem + env(safe-area-inset-bottom))" }}
    >
      <ul className="flex items-center justify-around">
        {tabs.map(({ key, to, label, Icon, badge, center, match }) => {
          const active = match(location.pathname);
          // Centered "Astro" tab is elevated into a filled pill
          if (center) {
            return (
              <li key={key} className="flex-1 flex justify-center">
                <Link
                  to={to}
                  aria-label={label}
                  aria-current={active ? "page" : undefined}
                  data-testid={`bottom-nav-${key}`}
                  className={`-mt-6 flex flex-col items-center justify-center w-14 h-14 rounded-2xl shadow-[0_4px_12px_rgba(110,15,44,0.4)] text-white transition-colors ${
                    active ? "bg-primary-dark" : "bg-primary"
                  }`}
                >
                  <Icon className="w-6 h-6" strokeWidth={2} />
                  <span className="text-[10px] font-semibold mt-0.5 leading-none">{label}</span>
                </Link>
              </li>
            );
          }
          return (
            <li key={key} className="flex-1">
              <Link
                to={to}
                aria-label={badge ? `${label} with ${badge} items` : label}
                aria-current={active ? "page" : undefined}
                data-testid={`bottom-nav-${key}`}
                className={`relative flex flex-col items-center justify-center py-1 px-2 rounded-xl transition-colors ${
                  active ? "text-primary bg-primary/10" : "text-gray-500 hover:text-primary"
                }`}
              >
                <Icon className="w-5 h-5" strokeWidth={active ? 2.25 : 2} aria-hidden="true" />
                <span className="text-[10px] font-medium mt-0.5 leading-none">{label}</span>
                {badge > 0 && (
                  <span
                    className="absolute top-0 right-1 min-w-[16px] h-4 px-1 rounded-full bg-red-600 text-white text-[9px] font-bold flex items-center justify-center"
                    aria-hidden="true"
                  >
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
