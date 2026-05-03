<?php
function adminer_object() {
    class AdminerPostgresOnly extends Adminer {
        function loginForm() {
            global $drivers;
            $drivers = array("pgsql" => "PostgreSQL");
            return parent::loginForm();
        }
    }
    return new AdminerPostgresOnly;
}
include 'adminer.php';
