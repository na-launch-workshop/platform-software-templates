<?php
if (!isset($_GET['pgsql']) && !isset($_POST['auth'])) {
    header('Location: ?pgsql=localhost');
    exit;
}
include 'adminer.php';
