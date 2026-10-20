# ---------------------------------------------------------------------------
#  Free Fire data source  (refatbd/free-fire-info-starter)
#  Installs the Laravel app at build time, so this folder is ALL you deploy —
#  no fork, no local PHP, no repo to maintain.
# ---------------------------------------------------------------------------
FROM php:8.3-apache

RUN apt-get update && apt-get install -y --no-install-recommends \
      git unzip libzip-dev libicu-dev libpng-dev libwebp-dev \
      libmagickwand-dev imagemagick \
 && docker-php-ext-configure gd --with-webp \
 && docker-php-ext-install zip intl gd \
 && pecl install imagick && docker-php-ext-enable imagick \
 && a2enmod rewrite headers \
 && rm -rf /var/lib/apt/lists/*

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
COPY php.ini /usr/local/etc/php/conf.d/zz-app.ini
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /var/www/html

# Build-time install of the starter app (no credentials baked in — those come
# from Render env vars at runtime).
RUN composer create-project refatbd/free-fire-info-starter . --no-dev \
      --no-interaction --prefer-dist \
 && mkdir -p storage/framework/{cache,sessions,views} storage/logs bootstrap/cache \
 && chown -R www-data:www-data storage bootstrap/cache

ENV APACHE_DOCUMENT_ROOT=/var/www/html/public
RUN sed -ri 's!/var/www/html!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/sites-available/*.conf \
 && sed -ri 's!/var/www/html!${APACHE_DOCUMENT_ROOT}!g' /etc/apache2/apache2.conf \
 && printf '<Directory /var/www/html/public>\n    AllowOverride All\n    Require all granted\n</Directory>\n' \
      > /etc/apache2/conf-available/app.conf \
 && a2enconf app

EXPOSE 80
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["apache2-foreground"]
