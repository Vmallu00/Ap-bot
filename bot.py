import os
import discord
from discord.ext import commands
import paramiko
from datetime import datetime

# ---------- Environment ----------
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))          # Your Discord user ID
SSH_HOST = os.getenv("SSH_HOST")               # VPS IP or domain
SSH_USER = os.getenv("SSH_USER", "root")
SSH_KEY_PATH = "/app/ssh_key"                  # Mounted private key file

# ---------- Docker Settings ----------
BASE_IMAGE = "ubuntu:22.04"
CONTAINER_PREFIX = "user_"

# ---------- SSH Helpers ----------
def run_ssh_command(command, timeout=30):
    """Run a command on the VPS via SSH, return (stdout, stderr)."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    private_key = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
    ssh.connect(SSH_HOST, username=SSH_USER, pkey=private_key, timeout=10)
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode('utf-8').strip()
    err = stderr.read().decode('utf-8').strip()
    ssh.close()
    return out, err

def docker_command(cmd):
    """Execute a Docker command on the VPS."""
    full_cmd = f"docker {cmd}"
    out, err = run_ssh_command(full_cmd)
    if err and "Error" in err:
        raise Exception(err)
    return out

def container_exists(name):
    out, _ = run_ssh_command(f"docker ps -a --filter name={name} --format '{{{{.Names}}}}'")
    return name in out

def get_user_container(user_id):
    name = f"{CONTAINER_PREFIX}{user_id}"
    out, _ = run_ssh_command(f"docker ps -a --filter name=^{name}$ --format '{{{{.Names}}}}'")
    return out if out == name else None

def get_container_stats(container_name):
    """Return stats dict for a container."""
    # Get stats
    stats_cmd = f"docker stats --no-stream --format '{{{{.Name}}}},{{{{.CPUPerc}}}},{{{{.MemPerc}}}},{{{{.MemUsage}}}},{{{{.NetIO}}}},{{{{.BlockIO}}}}' {container_name}"
    out, err = run_ssh_command(stats_cmd)
    if not out:
        return None
    parts = out.split(',')
    if len(parts) < 6:
        return None

    # Status
    status_cmd = f"docker inspect {container_name} --format='{{{{.State.Status}}}}'"
    status_out, _ = run_ssh_command(status_cmd)

    # Memory usage
    mem_usage = parts[3]
    if '/' in mem_usage:
        mem_used, mem_total = mem_usage.split('/')
    else:
        mem_used, mem_total = "N/A", "N/A"

    # Disk usage (from `docker ps -s`)
    size_cmd = f"docker ps -s --filter name={container_name} --format '{{{{.Size}}}}'"
    size_out, _ = run_ssh_command(size_cmd)

    return {
        "status": status_out.strip(),
        "cpu": parts[1],
        "mem_percent": parts[2],
        "mem_used": mem_used.strip(),
        "mem_total": mem_total.strip(),
        "disk": size_out.strip() if size_out else "N/A",
        "container_name": container_name
    }

# ---------- Discord Bot ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ---------- Commands ----------

@bot.command(name="ping")
@is_owner()
async def ping(ctx):
    """Owner-only ping command."""
    await ctx.send("Pong!")

@bot.command(name="create")
async def create_container(ctx, cpu: float, ram_gb: int, disk_gb: int):
    """!create <cpu> <ram_GB> <disk_GB>
    Example: !create 1 1 10  → 1 CPU core, 1 GB RAM, 10 GB disk
    """
    user = ctx.author
    container_name = f"{CONTAINER_PREFIX}{user.id}"

    if container_exists(container_name):
        await ctx.send(f"You already have a container. Use `!manage` to control it.")
        return

    ram = f"{ram_gb}G"
    disk = f"{disk_gb}G"

    # Create a container that stays alive (sleep infinity)
    cmd = (f"docker run -d --name {container_name} "
           f"--cpus={cpu} --memory={ram} --storage-opt size={disk} "
           f"{BASE_IMAGE} sleep infinity")

    try:
        docker_command(cmd)
        embed = discord.Embed(
            title="✅ Container Created!",
            description=f"Your container is now provisioned.",
            color=discord.Color.green()
        )
        embed.add_field(name="CPU", value=f"{cpu} core(s)", inline=True)
        embed.add_field(name="RAM", value=ram, inline=True)
        embed.add_field(name="Disk", value=disk, inline=True)
        embed.set_footer(text="Use !manage to view stats and control your container.")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Failed to create container: {e}")

@bot.command(name="delete")
async def delete_container(ctx, user: discord.Member = None):
    """Delete a user's container. Only admins or owner can delete."""
    if ctx.author.id != OWNER_ID and not ctx.author.guild_permissions.administrator:
        await ctx.send("Only administrators or the bot owner can delete containers.")
        return

    target = user or ctx.author
    container_name = get_user_container(target.id)
    if not container_name:
        await ctx.send(f"{target.mention} does not have a container.")
        return

    try:
        docker_command(f"rm -f {container_name}")
        await ctx.send(f"🗑️ Container for {target.mention} deleted.")
    except Exception as e:
        await ctx.send(f"❌ Failed to delete: {e}")

@bot.command(name="manage")
async def manage_container(ctx, user: discord.Member = None):
    """Show container stats and control buttons (Start/Stop/SSH)."""
    target = user or ctx.author

    # Only owner or the container owner can manage
    if target.id != ctx.author.id and ctx.author.id != OWNER_ID:
        await ctx.send("You can only manage your own container.")
        return

    container_name = get_user_container(target.id)
    if not container_name:
        await ctx.send(f"{target.mention} does not have a container. Use `!create`.")
        return

    stats = get_container_stats(container_name)
    if not stats:
        await ctx.send("Could not fetch container stats. Is the container running?")
        return

    # Build embed
    embed = discord.Embed(
        title=f"🖥️ {target.display_name}'s Container",
        color=0x00ff00 if stats['status'] == 'running' else 0xff0000,
        timestamp=datetime.now()
    )
    embed.add_field(name="Status", value=f"`{stats['status']}`", inline=True)
    embed.add_field(name="CPU", value=stats['cpu'], inline=True)
    embed.add_field(name="RAM", value=f"{stats['mem_used']} / {stats['mem_total']}", inline=True)
    embed.add_field(name="Disk Used", value=stats['disk'], inline=True)
    embed.set_footer(text="Buttons expire after 2 minutes.")

    # Create view with buttons
    view = discord.ui.View(timeout=120)

    # Toggle (Start/Stop) button
    if stats['status'] == 'running':
        toggle_label = "⏹️ Stop"
        toggle_style = discord.ButtonStyle.danger
    else:
        toggle_label = "▶️ Start"
        toggle_style = discord.ButtonStyle.success

    toggle_btn = discord.ui.Button(label=toggle_label, style=toggle_style, custom_id=f"toggle_{container_name}")

    # SSH (tmate) button
    ssh_btn = discord.ui.Button(label="🔗 SSH (tmate)", style=discord.ButtonStyle.primary, custom_id=f"ssh_{container_name}")

    # ---------- Button Callbacks ----------
    async def toggle_callback(interaction):
        # Authorization
        if interaction.user.id != target.id and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("You are not authorized.", ephemeral=True)
            return

        try:
            if stats['status'] == 'running':
                docker_command(f"stop {container_name}")
                new_status = "stopped"
            else:
                docker_command(f"start {container_name}")
                new_status = "running"

            await interaction.response.send_message(f"Container is now **{new_status}**.", ephemeral=True)
            # Refresh the manage view
            await ctx.invoke(manage_container, user=target)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def ssh_callback(interaction):
        if interaction.user.id != target.id and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        # Install tmate inside the container (if missing) and generate a session
        try:
            # We run a command that installs tmate if not present, then runs `tmate -F` to show the session link.
            ssh_cmd = (
                f"docker exec {container_name} bash -c "
                f"'which tmate >/dev/null || (apt-get update && apt-get install -y tmate) && "
                f"tmate -F'"
            )
            out, err = run_ssh_command(ssh_cmd, timeout=60)
            # tmate -F outputs something like "ssh session: ssh ..." – we send the whole output.
            await interaction.response.send_message(f"🔗 tmate session details:\n```\n{out}\n```", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to start tmate: {e}", ephemeral=True)

    toggle_btn.callback = toggle_callback
    ssh_btn.callback = ssh_callback

    view.add_item(toggle_btn)
    view.add_item(ssh_btn)

    await ctx.send(embed=embed, view=view)

@bot.command(name="list")
@is_owner()
async def list_containers(ctx):
    """List all user containers (owner only)."""
    out, _ = run_ssh_command(f"docker ps -a --filter name={CONTAINER_PREFIX} --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'")
    await ctx.send(f"```\n{out}\n```")

# ---------- Run Bot ----------
if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is required.")
    bot.run(TOKEN)
